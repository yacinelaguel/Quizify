"""
workers.py — Quizify background pipeline
PDF text extraction → Gemini quiz generation → DB persistence.
Fully async, designed for Render free tier (no timeouts, no blocking).
"""

import os
import re
import io
import json
import logging
import asyncio
import hashlib
from datetime import datetime
from typing import Optional

import httpx
from pypdf import PdfReader

from database import (
    SessionLocal, Document, Quiz, TASK_STATE, sha256_hex
)

logger = logging.getLogger("quizify.workers")

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────
AQ.Ab8RN6IsunPWbCzC2keE5goFN0RJNDvxuTwvCYQtPGZSEpCZgQ
GEMINI_MODEL    = "gemini-1.5-flash-latest"
GEMINI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

MAX_CONTEXT_CHARS  = 30_000   # safe Gemini context budget
MIN_CONTEXT_CHARS  = 200      # reject empty/corrupt PDFs below this
HTTP_TIMEOUT_SECS  = 120      # generous timeout for Gemini on slow free tier

# ─────────────────────────────────────────────────────────────────────────────
#  LEVEL DETECTION
# ─────────────────────────────────────────────────────────────────────────────
LEVEL_SIGNALS: dict[str, list[str]] = {
    "BAC": [
        "baccalauréat", "bac ", "ثانوية", "بكالوريا", "terminale",
        "lycée", "3ème année secondaire", "3as", "شعبة",
    ],
    "BEM": [
        "bem", "brevet d'enseignement moyen", "شهادة التعليم المتوسط",
        "متوسطة", "4ème année moyenne", "4am", "تعليم متوسط",
    ],
    "University": [
        "université", "جامعة", "master", "licence", "doctorat",
        "module ", "td ", "tp ", "cours magistral", "semestre",
        "محاضرة", "أعمال موجهة", "أعمال تطبيقية",
    ],
    "Concours": [
        "concours", "مسابقة", "médecine", "طب", "pharmacie", "architecture",
    ],
}

LANG_SIGNALS = {
    "ar": ["و", "في", "من", "على", "أن", "هو", "هي", "ما", "لا", "إلى"],
    "fr": ["le", "la", "les", "de", "du", "un", "une", "est", "sont", "dans"],
}


def detect_level(text: str) -> str:
    lower = text.lower()
    scores = {level: 0 for level in LEVEL_SIGNALS}
    for level, kws in LEVEL_SIGNALS.items():
        for kw in kws:
            scores[level] += lower.count(kw)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "General"


def detect_lang(text: str) -> str:
    ar_hits = sum(1 for w in LANG_SIGNALS["ar"] if f" {w} " in text)
    fr_hits = sum(1 for w in LANG_SIGNALS["fr"] if f" {w} " in text)
    if ar_hits > 0 and fr_hits > 0:
        return "mixed"
    if ar_hits > fr_hits:
        return "ar"
    return "fr"


# ─────────────────────────────────────────────────────────────────────────────
#  PDF EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def extract_pdf_text(file_bytes: bytes) -> tuple[str, int]:
    """
    Extract raw text from PDF bytes using pypdf.
    Returns (text_with_page_markers, page_count).
    Handles scanned/empty pages gracefully.
    """
    reader = PdfReader(io.BytesIO(file_bytes))
    pages  = []
    for i, page in enumerate(reader.pages):
        try:
            raw = page.extract_text() or ""
            raw = raw.strip()
        except Exception:
            raw = ""
        if raw:
            pages.append(f"[PAGE {i+1}]\n{raw}")

    full_text  = "\n\n".join(pages)
    page_count = len(reader.pages)
    return full_text, page_count


def clean_text(text: str) -> str:
    """Light normalisation — remove excessive whitespace and noise."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{3,}", " ", text)
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
#  PROMPT TEMPLATES
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_RULES = """\
You are Quizify's internal quiz engine. Your only job is to generate multiple-choice exam questions.

ABSOLUTE RULES — never break these:
1. CONTEXT LOCK: Use ONLY information that appears explicitly in the [DOCUMENT CONTEXT] section.
2. NO HALLUCINATION: If a concept is absent from the document, set its explanation to exactly:
   "هذه المعلومة غير متوفرة في المستندات المرفوعة"
3. OUTPUT: Respond with a raw JSON array ONLY. Zero preamble. Zero markdown fences. Zero explanation outside the array.
4. LANGUAGE: Write questions and options in the same language as the document.
   Every explanation must include at least one sentence in Arabic.
5. QUALITY: No duplicate questions. Each question must test a distinct concept.
6. OPTIONS: Always exactly 4 options (A, B, C, D). One correct answer. No "all of the above".
"""

def _build_regular_prompt(context: str, level: str, count: int, lang: str) -> str:
    lang_note = {
        "ar":    "Write questions entirely in Arabic.",
        "fr":    "Write questions in French. Explanations must include Arabic.",
        "mixed": "Mirror the document language (French + Arabic). Explanations include Arabic.",
    }.get(lang, "")

    return f"""{_SYSTEM_RULES}

[ACADEMIC LEVEL]: {level}
[LANGUAGE NOTE]: {lang_note}
[DOCUMENT CONTEXT]:
{context}

[TASK]:
Generate exactly {count} multiple-choice questions from the document context above.

Difficulty distribution:
- {max(1, count//4)} easy questions (direct recall)
- {max(1, count//2)} medium questions (application/comparison)
- {max(1, count//4)} hard questions (analysis, edge cases, traps)
- At least 2 questions must be classic Algerian exam traps

Each question object MUST follow this exact JSON schema:
{{
  "id": <integer starting at 1>,
  "question": "<question text>",
  "options": ["A. ...", "B. ...", "C. ...", "D. ..."],
  "answer": "<exact full correct option, e.g. A. ...>",
  "explanation": "<why correct — must reference document content — include Arabic sentence>",
  "is_trap": <true|false>,
  "trap_breakdown": "<null OR detailed explanation of the trap mechanism in Arabic>",
  "page_hint": <page number integer from PAGE markers, or null>
}}

Output the JSON array only. No other text.
"""


def _build_cram_prompt(context: str, level: str, lang: str) -> str:
    lang_note = {
        "ar":    "Write questions entirely in Arabic.",
        "fr":    "Write questions in French. Explanations must include Arabic.",
        "mixed": "Mirror the document language. Explanations include Arabic.",
    }.get(lang, "")

    return f"""{_SYSTEM_RULES}

[ACADEMIC LEVEL]: {level}
[LANGUAGE NOTE]: {lang_note}
[DOCUMENT CONTEXT]:
{context}

[TASK — CRAM MODE]:
Generate EXACTLY 5 highly strategic questions targeting:
- Classic traps from official Algerian exam answer keys (barème)
- Double-negatives, near-identical options, and common student misconceptions
- Edge-case definitions teachers always test
- Concepts where students almost always lose points

ALL 5 questions MUST have:
  "is_trap": true
  "trap_breakdown": <detailed Arabic explanation of the trap and why students fall for it>

Same JSON schema as above. Output the array only.
"""


# ─────────────────────────────────────────────────────────────────────────────
#  GEMINI API CALL
# ─────────────────────────────────────────────────────────────────────────────

async def call_gemini(prompt: str) -> tuple[str, int]:
    """
    Call Gemini Flash and return (raw_text_response, token_count).
    Uses httpx async client — never blocks the event loop.
    Implements exponential backoff on 429/503.
    """
    if not GEMINI_API_KEY:
        raise ValueError(
            "GEMINI_API_KEY not configured. "
            "Add it to your .env file or Render environment variables."
        )

    url = f"{GEMINI_BASE_URL}?key={GEMINI_API_KEY}"
    payload = {
        "contents": [
            {"parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "temperature":     0.35,
            "topK":            40,
            "topP":            0.92,
            "maxOutputTokens": 8192,
            "candidateCount":  1,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH",       "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECS) as client:
                resp = await client.post(url, json=payload)

            if resp.status_code == 429 or resp.status_code == 503:
                wait = 8 * (attempt + 1)
                logger.warning(f"Gemini rate-limited ({resp.status_code}). Waiting {wait}s...")
                await asyncio.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            break

        except httpx.TimeoutException:
            if attempt == 2:
                raise TimeoutError("Gemini request timed out after 3 attempts.")
            await asyncio.sleep(5)
    else:
        raise RuntimeError("Gemini failed after 3 attempts.")

    candidates = data.get("candidates", [])
    if not candidates:
        finish = data.get("promptFeedback", {}).get("blockReason", "unknown")
        raise ValueError(f"Gemini returned no candidates. Block reason: {finish}")

    parts = candidates[0].get("content", {}).get("parts", [])
    if not parts:
        raise ValueError("Gemini candidate has no content parts.")

    raw_text    = parts[0].get("text", "")
    token_count = data.get("usageMetadata", {}).get("totalTokenCount", 0)
    return raw_text, token_count


# ─────────────────────────────────────────────────────────────────────────────
#  JSON PARSER — strips any Gemini markdown noise
# ─────────────────────────────────────────────────────────────────────────────

def parse_questions(raw: str) -> list[dict]:
    """
    Robustly parse a JSON array from Gemini output.
    Handles markdown fences, leading/trailing text, and minor JSON errors.
    """
    # Strip markdown fences
    cleaned = re.sub(r"```(?:json)?", "", raw, flags=re.IGNORECASE).strip().strip("`")

    # Find outermost JSON array
    start = cleaned.find("[")
    end   = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(
            f"No JSON array found in Gemini output. "
            f"First 300 chars: {raw[:300]!r}"
        )

    json_str  = cleaned[start:end+1]
    questions = json.loads(json_str)

    if not isinstance(questions, list) or len(questions) == 0:
        raise ValueError("Gemini returned an empty question array.")

    # Sanitise and re-index
    required_keys = {
        "id", "question", "options", "answer", "explanation", "is_trap"
    }
    cleaned_qs = []
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            continue
        # Fill missing keys with safe defaults
        q.setdefault("id",            i + 1)
        q.setdefault("question",      "")
        q.setdefault("options",       [])
        q.setdefault("answer",        "")
        q.setdefault("explanation",   "")
        q.setdefault("is_trap",       False)
        q.setdefault("trap_breakdown", None)
        q.setdefault("page_hint",     None)

        # Re-index cleanly
        q["id"] = i + 1

        # Ensure options is a list of 4
        if not isinstance(q["options"], list):
            q["options"] = []
        while len(q["options"]) < 4:
            q["options"].append("")

        cleaned_qs.append(q)

    if not cleaned_qs:
        raise ValueError("All questions were malformed after sanitisation.")

    return cleaned_qs


# ─────────────────────────────────────────────────────────────────────────────
#  PDF INGEST (sync, runs in thread executor)
# ─────────────────────────────────────────────────────────────────────────────

def ingest_pdf(file_bytes: bytes, filename: str, session_id: str, task_id: str) -> str:
    """
    Extract text from PDF bytes and persist a Document record.
    Returns the document ID. Designed to run in run_in_executor.
    """
    db = SessionLocal()
    try:
        raw_text, page_count = extract_pdf_text(file_bytes)
        raw_text = clean_text(raw_text)

        if len(raw_text) < MIN_CONTEXT_CHARS:
            raise ValueError(
                f"PDF '{filename}' has too little extractable text "
                f"({len(raw_text)} chars). "
                "It may be a scanned image PDF — please use a text-layer PDF."
            )

        file_hash = sha256_hex(file_bytes)
        lang_hint = detect_lang(raw_text[:5000])

        doc = Document(
            session_id   = session_id,
            filename     = filename,
            storage_hash = file_hash,
            page_count   = page_count,
            raw_text     = raw_text,
            char_count   = len(raw_text),
            lang_hint    = lang_hint,
            task_id      = task_id,
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        logger.info(
            f"[{task_id}] PDF ingested: '{filename}' "
            f"({page_count}p, {len(raw_text):,} chars, lang={lang_hint})"
        )
        return doc.id
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN BACKGROUND WORKER
# ─────────────────────────────────────────────────────────────────────────────

async def process_quiz_task(
    task_id:    str,
    session_id: str,
    doc_id:     str,
    mode:       str,
    count:      int,
):
    """
    Full async pipeline:
      1. Load document text from DB
      2. Build Gemini prompt
      3. Call Gemini (with retry)
      4. Parse JSON response
      5. Persist Quiz record
      6. Update TASK_STATE for polling
    """
    TASK_STATE[task_id] = {
        "status":    "processing",
        "quiz_id":   None,
        "error":     None,
        "questions": None,
        "level":     None,
        "mode":      mode,
    }

    db = SessionLocal()
    try:
        # ── 1. Load document ─────────────────────────────────────────
        doc = db.query(Document).filter(Document.id == doc_id).first()
        if not doc or not doc.raw_text:
            raise ValueError(f"Document {doc_id} not found or has no text.")

        raw_text    = doc.raw_text
        context     = raw_text[:MAX_CONTEXT_CHARS]
        level       = detect_level(raw_text)
        lang        = doc.lang_hint or detect_lang(raw_text[:5000])
        actual_count = 5 if mode == "cram" else count

        logger.info(
            f"[{task_id}] Building {mode} quiz — "
            f"{actual_count}q, level={level}, lang={lang}"
        )

        # ── 2. Create DB record (status=processing) ──────────────────
        quiz = Quiz(
            task_id        = task_id,
            session_id     = session_id,
            document_id    = doc_id,
            status         = "processing",
            mode           = mode,
            question_count = actual_count,
            detected_level = level,
        )
        db.add(quiz)
        db.commit()
        db.refresh(quiz)

        # ── 3. Build prompt ──────────────────────────────────────────
        if mode == "cram":
            prompt = _build_cram_prompt(context, level, lang)
        else:
            prompt = _build_regular_prompt(context, level, count, lang)

        # ── 4. Call Gemini ───────────────────────────────────────────
        raw_response, tokens = await call_gemini(prompt)
        logger.info(f"[{task_id}] Gemini responded — {tokens} tokens")

        # ── 5. Parse ─────────────────────────────────────────────────
        questions = parse_questions(raw_response)

        # ── 6. Persist ───────────────────────────────────────────────
        quiz.questions     = questions
        quiz.status        = "completed"
        quiz.completed_at  = datetime.utcnow()
        quiz.gemini_tokens = tokens
        db.commit()

        state = {
            "status":    "completed",
            "quiz_id":   quiz.id,
            "error":     None,
            "questions": questions,
            "level":     level,
            "mode":      mode,
        }
        TASK_STATE[task_id] = state

        logger.info(
            f"[{task_id}] ✓ Quiz completed — "
            f"{len(questions)} questions, level={level}"
        )

    except Exception as exc:
        err_msg = str(exc)
        logger.error(f"[{task_id}] ✗ Worker failed: {err_msg}", exc_info=True)

        # Mark quiz failed in DB if it was created
        try:
            failed = db.query(Quiz).filter(Quiz.task_id == task_id).first()
            if failed:
                failed.status        = "failed"
                failed.error_message = err_msg
                db.commit()
        except Exception:
            pass

        TASK_STATE[task_id] = {
            "status":    "failed",
            "quiz_id":   None,
            "error":     err_msg,
            "questions": None,
            "level":     None,
            "mode":      mode,
        }
    finally:
        db.close()
