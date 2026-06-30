"""
database.py — Quizify
Full SQLAlchemy layer: models, session factory, analytics engine, helpers.
SQLite is free, zero-config, and works perfectly on Render's free tier.
"""

import uuid
import json
import hashlib
from datetime import datetime, timedelta
from typing import Optional, List

from sqlalchemy import (
    create_engine, Column, String, Text, Integer,
    Float, DateTime, ForeignKey, Boolean, Index, event
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session
from sqlalchemy.pool import StaticPool

# ─────────────────────────────────────────────────────────────────────────────
#  ENGINE — SQLite with WAL mode for concurrency on Render free tier
# ─────────────────────────────────────────────────────────────────────────────
DATABASE_URL = "sqlite:///./quizify.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,
        "timeout": 30,
    },
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

# Enable WAL mode so reads don't block writes (crucial on free hosting)
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA cache_size=10000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ─────────────────────────────────────────────────────────────────────────────
#  MODELS
# ─────────────────────────────────────────────────────────────────────────────

class UserSession(Base):
    """
    Anonymous browser session tracked via a cookie UUID.
    No login required — completely free and private.
    """
    __tablename__ = "user_sessions"

    id             = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at     = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    total_quizzes  = Column(Integer, default=0, nullable=False)
    total_correct  = Column(Integer, default=0, nullable=False)
    total_answered = Column(Integer, default=0, nullable=False)
    cumul_score    = Column(Float,   default=0.0, nullable=False)  # running avg %
    streak_days    = Column(Integer, default=0, nullable=False)
    last_quiz_date = Column(DateTime, nullable=True)

    documents    = relationship("Document",        back_populates="session", cascade="all, delete-orphan")
    quizzes      = relationship("Quiz",            back_populates="session", cascade="all, delete-orphan")
    performances = relationship("UserPerformance", back_populates="session", cascade="all, delete-orphan")

    def update_streak(self):
        """Call after each completed quiz to keep streak count accurate."""
        today = datetime.utcnow().date()
        if self.last_quiz_date:
            last = self.last_quiz_date.date()
            if last == today:
                pass  # same day, no change
            elif last == today - timedelta(days=1):
                self.streak_days += 1  # consecutive day
            else:
                self.streak_days = 1   # broke streak, restart
        else:
            self.streak_days = 1
        self.last_quiz_date = datetime.utcnow()


class Document(Base):
    """
    Each uploaded PDF. Raw extracted text stored directly in SQLite
    (no external file storage needed — free tier friendly).
    """
    __tablename__ = "documents"

    id           = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id   = Column(String(36), ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False)
    filename     = Column(String(255), nullable=False)
    storage_hash = Column(String(64),  nullable=True)   # SHA-256 of file bytes
    page_count   = Column(Integer,     default=0)
    raw_text     = Column(Text,        nullable=True)   # full pypdf extraction
    char_count   = Column(Integer,     default=0)
    lang_hint    = Column(String(10),  default="ar")   # ar | fr | mixed
    uploaded_at  = Column(DateTime,    default=datetime.utcnow)
    task_id      = Column(String(36),  nullable=True, index=True)

    session = relationship("UserSession", back_populates="documents")
    quizzes = relationship("Quiz", back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_doc_session", "session_id"),
        Index("ix_doc_task",    "task_id"),
    )


class Quiz(Base):
    """
    A generated quiz. questions_json is the full serialised question array.

    Question schema (one item):
    {
      "id":            <int>,
      "question":      "<str>",
      "options":       ["A. ...", "B. ...", "C. ...", "D. ..."],
      "answer":        "A. ...",          # full correct option
      "explanation":   "<str>",
      "is_trap":       <bool>,
      "trap_breakdown": "<str | null>",
      "page_hint":     <int | null>
    }
    """
    __tablename__ = "quizzes"

    id             = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id        = Column(String(36), unique=True, nullable=False, index=True)
    session_id     = Column(String(36), ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False)
    document_id    = Column(String(36), ForeignKey("documents.id",     ondelete="CASCADE"), nullable=False)

    # status lifecycle: queued → processing → completed | failed
    status         = Column(String(20), default="queued",   nullable=False, index=True)
    mode           = Column(String(20), default="regular",  nullable=False)  # regular | cram
    question_count = Column(Integer,    default=10)
    detected_level = Column(String(30), nullable=True)   # BAC | BEM | University | General

    questions_json = Column(Text, nullable=True)
    error_message  = Column(Text, nullable=True)
    gemini_tokens  = Column(Integer, default=0)   # track usage

    created_at    = Column(DateTime, default=datetime.utcnow)
    completed_at  = Column(DateTime, nullable=True)

    session  = relationship("UserSession", back_populates="quizzes")
    document = relationship("Document",    back_populates="quizzes")
    performances = relationship("UserPerformance", back_populates="quiz", cascade="all, delete-orphan")

    @property
    def questions(self) -> list:
        if self.questions_json:
            try:
                return json.loads(self.questions_json)
            except Exception:
                return []
        return []

    @questions.setter
    def questions(self, value: list):
        self.questions_json = json.dumps(value, ensure_ascii=False, indent=None)

    __table_args__ = (
        Index("ix_quiz_session", "session_id"),
        Index("ix_quiz_status",  "status"),
    )


class UserPerformance(Base):
    """
    One record per completed quiz submission.
    wrong_answers_json stores a list of detailed wrong answer objects
    so we can render full breakdowns without re-fetching the quiz.
    """
    __tablename__ = "user_performances"

    id                = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id        = Column(String(36), ForeignKey("user_sessions.id", ondelete="CASCADE"), nullable=False)
    quiz_id           = Column(String(36), ForeignKey("quizzes.id",       ondelete="CASCADE"), nullable=False)

    score_pct         = Column(Float,   nullable=False)
    correct_count     = Column(Integer, nullable=False)
    total_count       = Column(Integer, nullable=False)
    time_taken_secs   = Column(Integer, nullable=True)
    mode              = Column(String(20), default="regular")

    wrong_answers_json = Column(Text, default="[]")
    """
    Each item:
    {
      "q_id":          <int>,
      "question":      "<str>",
      "chosen":        "<str>",
      "correct":       "<str>",
      "explanation":   "<str>",
      "is_trap":       <bool>,
      "trap_breakdown": "<str | null>"
    }
    """

    completed_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("UserSession", back_populates="performances")
    quiz    = relationship("Quiz",        back_populates="performances")

    @property
    def wrong_answers(self) -> list:
        try:
            return json.loads(self.wrong_answers_json or "[]")
        except Exception:
            return []

    @wrong_answers.setter
    def wrong_answers(self, value: list):
        self.wrong_answers_json = json.dumps(value, ensure_ascii=False)

    __table_args__ = (
        Index("ix_perf_session", "session_id"),
        Index("ix_perf_quiz",    "quiz_id"),
        Index("ix_perf_date",    "completed_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  IN-MEMORY TASK STATE (fast polling without constant DB hits)
# ─────────────────────────────────────────────────────────────────────────────
TASK_STATE: dict[str, dict] = {}
"""
Schema per entry:
{
  "status":    "queued" | "processing" | "completed" | "failed",
  "quiz_id":   "<str | None>",
  "error":     "<str | None>",
  "questions": [<question objects>],   # populated on completed
  "level":     "<str>",
  "mode":      "<str>",
}
"""


# ─────────────────────────────────────────────────────────────────────────────
#  DEPENDENCIES & HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_db():
    """FastAPI dependency — yields a managed DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables on first run. Safe to call multiple times."""
    Base.metadata.create_all(bind=engine)


def get_or_create_session(session_id: Optional[str], db: Session) -> "UserSession":
    """Return existing UserSession or mint a new anonymous one."""
    if session_id:
        us = db.query(UserSession).filter(UserSession.id == session_id).first()
        if us:
            us.last_seen = datetime.utcnow()
            db.commit()
            return us
    us = UserSession()
    db.add(us)
    db.commit()
    db.refresh(us)
    return us


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
#  ANALYTICS ENGINE
# ─────────────────────────────────────────────────────────────────────────────

def get_analytics(session_id: str, db: Session) -> dict:
    """
    Returns all stats needed by the dashboard in a single dict.
    Queries are intentionally lightweight for SQLite free tier.
    """
    perfs: List[UserPerformance] = (
        db.query(UserPerformance)
        .filter(UserPerformance.session_id == session_id)
        .order_by(UserPerformance.completed_at.asc())
        .all()
    )

    if not perfs:
        return _empty_analytics()

    scores     = [p.score_pct for p in perfs]
    total_q    = sum(p.total_count   for p in perfs)
    total_corr = sum(p.correct_count for p in perfs)

    # Score history (one point per quiz)
    history = []
    for p in perfs:
        history.append({
            "date":    p.completed_at.strftime("%Y-%m-%d"),
            "score":   round(p.score_pct, 1),
            "correct": p.correct_count,
            "total":   p.total_count,
            "mode":    p.mode,
        })

    # Level distribution (from linked quizzes)
    level_dist: dict[str, int] = {}
    for p in perfs:
        q = db.query(Quiz).filter(Quiz.id == p.quiz_id).first()
        if q and q.detected_level:
            level_dist[q.detected_level] = level_dist.get(q.detected_level, 0) + 1

    # Streak (days with at least one quiz)
    us = db.query(UserSession).filter(UserSession.id == session_id).first()

    # Most common wrong topics (from wrong answers across all quizzes)
    all_wrongs: list[dict] = []
    for p in perfs:
        all_wrongs.extend(p.wrong_answers)

    trap_count  = sum(1 for w in all_wrongs if w.get("is_trap"))
    total_wrong = len(all_wrongs)

    # Recent scores (last 10, for mini chart)
    recent_scores = [round(s, 1) for s in scores[-10:]]

    # Weekly performance (last 7 days)
    today      = datetime.utcnow().date()
    week_perf  = {}
    for p in perfs:
        day = p.completed_at.date()
        if (today - day).days < 7:
            ds = day.isoformat()
            week_perf.setdefault(ds, []).append(p.score_pct)
    weekly = {k: round(sum(v)/len(v), 1) for k, v in week_perf.items()}

    return {
        "total_quizzes":           len(perfs),
        "average_score":           round(sum(scores) / len(scores), 1),
        "best_score":              round(max(scores), 1),
        "worst_score":             round(min(scores), 1),
        "total_questions_answered": total_q,
        "total_correct":           total_corr,
        "accuracy_pct":            round((total_corr / total_q * 100), 1) if total_q else 0,
        "trap_questions_wrong":    trap_count,
        "total_wrong":             total_wrong,
        "streak_days":             getattr(us, "streak_days", 0),
        "history":                 history,
        "level_distribution":      level_dist,
        "recent_scores":           recent_scores,
        "weekly_avg":              weekly,
    }


def _empty_analytics() -> dict:
    return {
        "total_quizzes": 0, "average_score": 0, "best_score": 0,
        "worst_score": 0, "total_questions_answered": 0, "total_correct": 0,
        "accuracy_pct": 0, "trap_questions_wrong": 0, "total_wrong": 0,
        "streak_days": 0, "history": [], "level_distribution": {},
        "recent_scores": [], "weekly_avg": {},
    }
