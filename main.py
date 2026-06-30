"""
main.py — Quizify FastAPI entrypoint
All routes, CORS, session management, background task orchestration.
Render free tier compatible — no external storage, all-SQLite architecture.
"""

import os
import uuid
import asyncio
import logging
import random
from datetime import datetime
from typing import Optional, List

from fastapi import (
    FastAPI, UploadFile, File, Form, BackgroundTasks,
    Request, Response, Depends, HTTPException, Cookie
)
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import (
    init_db, get_db, get_or_create_session, get_analytics,
    UserSession, Document, Quiz, UserPerformance, TASK_STATE,
)
from workers import ingest_pdf, process_quiz_task

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(name)s │ %(levelname)s │ %(message)s",
)
logger = logging.getLogger("quizify.main")

# ─────────────────────────────────────────────────────────────────────────────
#  APP INIT
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Quizify",
    description="منصة جزائرية ذكية لتوليد الاختبارات من دروسك",
    version="2.0.0",
    docs_url="/docs",
    openapi_url="/openapi.json",
)

# ── CORS (allow any origin for free tier friendliness) ────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Templates (Jinja2 for index.html rendering) ──────────────────────────
templates = Jinja2Templates(directory="templates")

# ── Startup hooks ────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    init_db()
    logger.info("─" * 70)
    logger.info("🎯 Quizify v2.0 — منصة الاختبارات الذكية")
    logger.info("✓ Database initialised (SQLite + WAL mode)")
    logger.info("✓ FastAPI running in async mode")
    logger.info("✓ Render free tier compatible (no external storage)")
    logger.info("─" * 70)


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — PAGES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    response: Response,
    session_id: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
):
    """
    Main SPA page. Serves pre-rendered HTML with embedded JS.
    Avoids the 30-second HTTP timeout by:
      1. Returning HTML immediately (no rendering delay)
      2. All API calls are asynchronous and non-blocking
      3. Quiz generation happens in BackgroundTasks (off-thread)
    """
    us = get_or_create_session(session_id, db)
    response.set_cookie(
        "session_id",
        us.id,
        max_age=60*60*24*365,  # 1 year
        httponly=True,
        samesite="lax",
    )
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health_check():
    """Simple health check for uptime monitoring."""
    return JSONResponse({
        "status": "ok",
        "service": "quizify",
        "time": datetime.utcnow().isoformat(),
        "version": "2.0.0",
    })


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — FILE UPLOAD
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_pdfs(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    mode: str = Form(default="regular"),
    count: int = Form(default=10),
    session_id: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
):
    """
    Upload 1–20 PDFs. Returns immediately with task_ids.
    Client polls /api/quiz/status/{task_id} to track progress.

    All heavy lifting (PDF parsing + Gemini) runs in BackgroundTasks,
    never blocking the HTTP response.
    """
    # ── Validation ──────────────────────────────────────────────────────
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 PDFs per upload.")
    if len(files) == 0:
        raise HTTPException(status_code=400, detail="No files provided.")
    if mode not in ("regular", "cram"):
        raise HTTPException(status_code=400, detail="mode must be 'regular' or 'cram'.")
    if mode == "cram":
        count = 5  # Cram always uses 5 questions
    elif count not in (10, 20, 30):
        count = 10

    # ── Session ─────────────────────────────────────────────────────────
    us = get_or_create_session(session_id, db)

    # ── Process files ───────────────────────────────────────────────────
    task_ids = []
    for file in files:
        # Validation
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=400,
                detail=f"{file.filename} is not a PDF file."
            )
        file_bytes = await file.read()
        if len(file_bytes) == 0:
            raise HTTPException(
                status_code=400,
                detail=f"{file.filename} is empty."
            )
        if len(file_bytes) > 50 * 1024 * 1024:  # 50 MB limit
            raise HTTPException(
                status_code=413,
                detail=f"{file.filename} exceeds 50 MB."
            )

        # ── Create task ─────────────────────────────────────────────────
        task_id = str(uuid.uuid4())
        TASK_STATE[task_id] = {
            "status":    "queued",
            "quiz_id":   None,
            "error":     None,
            "questions": None,
            "level":     None,
            "mode":      mode,
        }

        # ── Queue background pipeline ───────────────────────────────────
        background_tasks.add_task(
            _run_full_pipeline,
            file_bytes,
            file.filename,
            us.id,
            task_id,
            mode,
            count,
        )
        task_ids.append(task_id)

    # ── Response with session cookie ────────────────────────────────────
    resp = JSONResponse({
        "task_ids": task_ids,
        "status": "queued",
        "message": "المعالجة بدأت في الخلفية...",
    })
    resp.set_cookie(
        "session_id",
        us.id,
        max_age=60*60*24*365,
        httponly=True,
        samesite="lax",
    )
    return resp


async def _run_full_pipeline(
    file_bytes: bytes,
    filename: str,
    session_id: str,
    task_id: str,
    mode: str,
    count: int,
):
    """
    Orchestrates the full async pipeline:
      1. Sync PDF extraction (runs in thread executor)
      2. Async Gemini quiz generation
    """
    try:
        # Step 1: Extract PDF (sync, run in thread pool to avoid blocking)
        loop = asyncio.get_event_loop()
        doc_id = await loop.run_in_executor(
            None,
            ingest_pdf,
            file_bytes,
            filename,
            session_id,
            task_id,
        )

        # Step 2: Generate quiz (async, can await)
        await process_quiz_task(
            task_id=task_id,
            session_id=session_id,
            doc_id=doc_id,
            mode=mode,
            count=count,
        )
    except Exception as exc:
        logger.error(f"[{task_id}] Pipeline failed: {exc}", exc_info=True)
        TASK_STATE[task_id] = {
            "status":    "failed",
            "quiz_id":   None,
            "error":     str(exc),
            "questions": None,
            "level":     None,
            "mode":      mode,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — POLLING
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/quiz/status/{task_id}")
async def get_quiz_status(task_id: str, db: Session = Depends(get_db)):
    """
    Poll this endpoint to check quiz generation status.
    Frontend calls every 2 seconds until completion.

    Returns:
      { status, quiz_id, error, questions, level, mode }
    """
    state = TASK_STATE.get(task_id)
    if state is None:
        # Fallback: check DB (e.g., after server restart)
        quiz = db.query(Quiz).filter(Quiz.task_id == task_id).first()
        if not quiz:
            raise HTTPException(
                status_code=404,
                detail=f"Task {task_id} not found."
            )
        state = {
            "status":    quiz.status,
            "quiz_id":   quiz.id,
            "error":     quiz.error_message,
            "questions": quiz.questions if quiz.status == "completed" else None,
            "level":     quiz.detected_level,
            "mode":      quiz.mode,
        }

    return JSONResponse(content=state)


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — QUIZ SUBMISSION & GRADING
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/quiz/{quiz_id}/submit")
async def submit_quiz_answers(
    quiz_id: str,
    request: Request,
    session_id: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
):
    """
    User submits their answers. We grade and return results immediately.
    All grading logic is local (no external API calls).

    Request body: { answers: {q_id: "A. ...", ...}, time_taken_secs: 120 }
    """
    body = await request.json()
    user_answers: dict = body.get("answers", {})
    time_taken: int = body.get("time_taken_secs", 0)

    # ── Load quiz ───────────────────────────────────────────────────────
    quiz = db.query(Quiz).filter(Quiz.id == quiz_id).first()
    if not quiz:
        raise HTTPException(status_code=404, detail="Quiz not found.")
    if quiz.status != "completed":
        raise HTTPException(
            status_code=400,
            detail="Quiz is not ready yet."
        )

    # ── Load session ────────────────────────────────────────────────────
    us = get_or_create_session(session_id, db)

    # ── Grade ───────────────────────────────────────────────────────────
    questions = quiz.questions
    correct   = 0
    wrong_details = []

    for q in questions:
        q_id_str   = str(q["id"])
        user_ans   = user_answers.get(q_id_str, "").strip()
        correct_ans = (q.get("answer") or "").strip()

        # Simple matching: check first letter of option
        is_correct = (
            user_ans.upper().startswith(correct_ans[0].upper())
            if correct_ans else False
        )

        if is_correct:
            correct += 1
        else:
            wrong_details.append({
                "q_id":           q["id"],
                "question":       q.get("question", ""),
                "chosen":         user_ans or "(no answer)",
                "correct":        correct_ans,
                "explanation":    q.get("explanation", ""),
                "is_trap":        q.get("is_trap", False),
                "trap_breakdown": q.get("trap_breakdown"),
            })

    total     = len(questions)
    score_pct = round((correct / total) * 100, 1) if total > 0 else 0

    # ── Persist performance ─────────────────────────────────────────────
    perf = UserPerformance(
        session_id      = us.id,
        quiz_id         = quiz_id,
        score_pct       = score_pct,
        correct_count   = correct,
        total_count     = total,
        time_taken_secs = time_taken,
        mode            = quiz.mode,
    )
    perf.wrong_answers = wrong_details
    db.add(perf)

    # ── Update session stats ────────────────────────────────────────────
    us.total_quizzes    += 1
    us.total_correct    += correct
    us.total_answered   += total
    us.cumul_score      = (us.cumul_score * (us.total_quizzes - 1) + score_pct) / us.total_quizzes
    us.update_streak()

    db.commit()

    # ── Pick feedback message ───────────────────────────────────────────
    feedback = _pick_feedback(score_pct)

    return JSONResponse(content={
        "score_pct":      score_pct,
        "correct":        correct,
        "total":          total,
        "wrong_answers":  wrong_details,
        "feedback":       feedback,
        "performance_id": perf.id,
        "time_taken":     time_taken,
    })


def _pick_feedback(score: float) -> dict:
    """Return contextual feedback message based on performance tier."""
    if score >= 85:
        msgs = [
            "wait, are you... single? because that score is attractive. 🫣💍",
            "wanna come over and explain the whole course to me? 🫠🫦",
            "if you're this good at everything, i might need to look away. 👀😏",
            "how are you so good at this? you're making me look bad. 🤤",
            "literally perfect. marry me??? 💕",
        ]
    elif score >= 70:
        msgs = [
            "solid performance. i'm impressed, not gonna lie. 😎",
            "you're getting the hang of this. keep pushing. 💪",
            "not bad at all. you've got potential. ⭐",
        ]
    elif score >= 50:
        msgs = [
            "okay, you got some right. that's a start. 🤷",
            "not the worst, but let's aim higher next time. 📈",
            "you tried, and that's what matters. keep going. 🙌",
        ]
    else:
        msgs = [
            "let's just go get food and pretend this never happened. 🫣",
            "i'm just gonna look at your previous score. this one is glitching. 👀",
            "you tried, that's almost cute. :3",
            "it's okay, i still like you. maybe less now, but still. 🫠",
            "did you even open the pdf or are you just guessing? -.-",
            "i expected better from you tbh. -.-",
        ]
    return {
        "message": random.choice(msgs),
        "tier": "high" if score >= 70 else "low",
    }


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — ANALYTICS & DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/analytics")
async def get_analytics(
    session_id: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
):
    """Return full dashboard analytics for the user's session."""
    if not session_id:
        return JSONResponse({
            "error": "no_session",
            "total_quizzes": 0,
        })

    us = db.query(UserSession).filter(UserSession.id == session_id).first()
    if not us:
        return JSONResponse({
            "error": "session_not_found",
            "total_quizzes": 0,
        })

    data = get_analytics(session_id, db)
    return JSONResponse(content=data)


@app.get("/api/history")
async def get_quiz_history(
    session_id: Optional[str] = Cookie(default=None),
    db: Session = Depends(get_db),
    limit: int = 20,
):
    """Get recent quizzes from the user's session."""
    if not session_id:
        return JSONResponse({"quizzes": []})

    quizzes = (
        db.query(Quiz)
        .filter(Quiz.session_id == session_id, Quiz.status == "completed")
        .order_by(Quiz.completed_at.desc())
        .limit(limit)
        .all()
    )

    return JSONResponse(content={
        "quizzes": [
            {
                "id":              q.id,
                "task_id":         q.task_id,
                "mode":            q.mode,
                "detected_level":  q.detected_level,
                "question_count":  q.question_count,
                "completed_at":    q.completed_at.isoformat() if q.completed_at else None,
                "gemini_tokens":   q.gemini_tokens,
            }
            for q in quizzes
        ]
    })


# ─────────────────────────────────────────────────────────────────────────────
#  ROUTES — MISC
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/stats/global")
async def global_stats(db: Session = Depends(get_db)):
    """Public stats (optional feature for social proof on homepage)."""
    try:
        total_users = db.query(UserSession).count()
        total_quizzes = db.query(Quiz).filter(Quiz.status == "completed").count()
        total_perfs = db.query(UserPerformance).count()

        avg_score = 0.0
        if total_perfs > 0:
            avg = db.query(UserPerformance).with_entities(
                db.func.avg(UserPerformance.score_pct)
            ).scalar() or 0
            avg_score = round(avg, 1)

        return JSONResponse({
            "total_users":   total_users,
            "total_quizzes": total_quizzes,
            "total_answers": total_perfs,
            "avg_score":     avg_score,
        })
    except Exception as e:
        logger.error(f"Global stats error: {e}")
        return JSONResponse({"error": "unavailable"}, status_code=500)


@app.post("/api/feedback")
async def submit_feedback(
    request: Request,
    session_id: Optional[str] = Cookie(default=None),
):
    """User feedback endpoint (optional)."""
    try:
        body = await request.json()
        feedback = body.get("message", "")
        rating = body.get("rating", 0)

        logger.info(f"Feedback from {session_id}: rating={rating}, msg={feedback[:50]}")

        return JSONResponse({
            "success": True,
            "message": "شكراً على آرائك! 💙",
        })
    except Exception as e:
        logger.error(f"Feedback error: {e}")
        return JSONResponse({"error": "failed"}, status_code=400)


# ─────────────────────────────────────────────────────────────────────────────
#  ERROR HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "server_error",
            "message": "حدث خطأ غير متوقع. حاول مرة أخرى.",
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
#  RUN (development)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("ENV", "development") == "development",
        log_level="info",
    )
