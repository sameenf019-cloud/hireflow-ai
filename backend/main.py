"""
main.py — FastAPI wrapper around the HireFlow CrewAI pipeline.

Endpoints
---------
GET  /health
POST /jobs                              upload the JD (pdf/docx/txt)       -> job_id
POST /candidates                        upload resumes (stage 1 ingestion) -> per-file results
GET  /candidates                        list candidates + parsed results
GET  /candidates/{email}                one candidate
POST /candidates/{email}/run            route ONE candidate by status (background)  -> run_id
POST /pipeline/run                      screen every PENDING_SCREENING candidate    -> run_id
POST /poll                              Gmail poll + reschedule routing             -> run_id
POST /candidates/{email}/evaluate       stage 5 with manager's notes                -> run_id
GET  /runs/{run_id}                     run status + results
GET  /runs/{run_id}/events              Server-Sent Events: live agent-handoff feed
GET  /candidates/{email}/evaluation/stream   SSE: final summary word by word

Demo mode (GOOGLE_MOCK_MODE=true only)
GET  /demo/status                       is demo available / are samples loaded
POST /demo/load                         load sample job description + sample candidates
POST /demo/reply                        simulate replies (a different time per candidate)
POST /demo/reset                        remove sample candidates, their mock emails
                                        AND their mock calendar events

Runs execute in a worker thread (CrewAI is blocking) and are serialised by a
global lock: one pipeline run at a time keeps SQLite writes and Groq rate
limits predictable.

Run:  uvicorn main:app --reload --port 8000
Needs (add to requirements.txt if missing): fastapi, uvicorn[standard], python-multipart

------------------------------------------------------------------------
ASSUMED INTERFACES (adapter block below is the only place they are used)
------------------------------------------------------------------------
rag_engine.extract_text(filename: str, data: bytes) -> str
rag_engine.index_resume(candidate_email: str, text: str, filename: str) -> int
state_manager.init_db()
state_manager.get_candidate(email) / list_candidates(status=None)
state_manager.create_candidate(email, name=None, resume_filename=None) -> None
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import config
import orchestrator as orch
import demo_data
import rag_engine
import state_manager as sm

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("hireflow.api")

ALLOWED_EXTS = {".pdf", ".docx", ".txt"}
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB per file

DATA_DIR = Path(getattr(config, "DATA_DIR", None) or os.getenv("DATA_DIR") or "data")
JOBS_DIR = DATA_DIR / "jobs"


# ==========================================================================
# Adapter block
# ==========================================================================
def _extract_text(filename: str, data: bytes) -> str:
    """rag_engine.extract_text wants a file path, so write the upload to a temp file first."""
    suffix = Path(filename).suffix.lower() or ".txt"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        return rag_engine.extract_text(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _current_job_id() -> int:
    """Latest job row in SQLite (newest first); creates an empty one if no JD was uploaded yet."""
    jobs = sm.list_jobs()
    if jobs:
        return jobs[0]["id"]
    return sm.create_job(title="Unassigned", jd_text="", filename=None)


def _create_candidate(email: str, name: Optional[str], filename: str, text: str) -> Tuple[int, int]:
    job_id = _current_job_id()
    cand_id = sm.create_candidate(
        job_id=job_id, email=email, resume_text=text, name=name, resume_filename=filename
    )
    return cand_id, job_id


def _index_resume(candidate_id: int, job_id: int, email: str, text: str, filename: str) -> int:
    return rag_engine.index_resume(candidate_id, job_id, email, text, filename)


def _email_of(cand: dict) -> str:
    return cand["email"] if "email" in cand else cand["candidate_email"]


# ==========================================================================
# Job description storage (plain files; one JD per upload, "latest" wins)
# ==========================================================================
def _save_jd(text: str) -> str:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    (JOBS_DIR / f"{job_id}.txt").write_text(text, encoding="utf-8")
    return job_id


def _load_jd(job_id: Optional[str] = None) -> Optional[str]:
    if not JOBS_DIR.exists():
        return None
    if job_id:
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):  # no path tricks
            return None
        path = JOBS_DIR / f"{job_id}.txt"
        return path.read_text(encoding="utf-8") if path.exists() else None
    files = sorted(JOBS_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].read_text(encoding="utf-8") if files else None


# ==========================================================================
# Run store: events are appended from worker threads, read by SSE endpoints
# ==========================================================================
class RunStore:
    def __init__(self, keep: int = 50) -> None:
        self._runs: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._keep = keep

    def create(self, kind: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._runs[run_id] = {
                "run_id": run_id, "kind": kind, "events": [], "done": False,
                "results": None, "error": None, "created": time.time(),
            }
            for old in sorted(self._runs, key=lambda r: self._runs[r]["created"])[:-self._keep]:
                del self._runs[old]
        return run_id

    def emit(self, run_id: str, event: dict) -> None:
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id]["events"].append(event)

    def finish(self, run_id: str, results: Any, error: Optional[str] = None) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return
            run["events"].append({
                "ts": orch._now_iso(), "stage": "complete", "agent": "Orchestrator",
                "message": "Run failed." if error else "Run finished.", "ok": error is None,
            })
            run["results"], run["error"], run["done"] = results, error, True

    def snapshot(self, run_id: str, start: int) -> Optional[Tuple[List[dict], bool]]:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            return list(run["events"][start:]), run["done"]

    def get(self, run_id: str) -> Optional[dict]:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            return {k: v for k, v in run.items() if k != "events"} | {"event_count": len(run["events"])}


RUNS = RunStore()
EXECUTOR = ThreadPoolExecutor(max_workers=4)
PIPELINE_LOCK = threading.Lock()  # one CrewAI run at a time


def _launch(kind: str, work: Callable[[Callable[[dict], None]], Any]) -> str:
    run_id = RUNS.create(kind)

    def job() -> None:
        with PIPELINE_LOCK:
            try:
                results = work(lambda event: RUNS.emit(run_id, event))
                RUNS.finish(run_id, results)
            except Exception as exc:
                log.exception("Run %s (%s) failed", run_id, kind)
                RUNS.finish(run_id, None, error=str(exc))

    EXECUTOR.submit(job)
    return run_id


# ==========================================================================
# App
# ==========================================================================
@asynccontextmanager
async def lifespan(_: FastAPI):
    sm.init_db()
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="HireFlow AI", version="1.0.0", lifespan=lifespan)

_origins = os.getenv("FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- helpers ---------------------------------------------------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _find_email(text: str) -> Optional[str]:
    match = _EMAIL_RE.search(text or "")
    return match.group(0).lower() if match else None


# Lines that look like a name but are really a section heading or a job title.
_NOT_NAME_LINES = {
    "professional summary", "summary", "profile", "career objective", "objective",
    "personal statement", "about me", "contact", "contact information", "curriculum vitae",
    "resume", "cv", "experience", "work experience", "professional experience",
    "employment history", "education", "skills", "technical skills", "key skills",
    "core skills", "projects", "certifications", "languages", "references",
    "achievements", "awards", "interests", "hobbies",
}
# If any word of a line is one of these, the line is a job title, not a person's name.
_ROLE_WORDS = {
    "developer", "engineer", "designer", "editor", "analyst", "manager", "consultant",
    "specialist", "architect", "administrator", "scientist", "intern", "officer",
    "executive", "assistant", "lead", "director", "programmer", "technician",
    "summary", "experience", "education", "skills", "projects", "objective", "profile",
}
_NAME_LINE_RE = re.compile(r"[A-Za-z][A-Za-z.'\-]*(?: [A-Za-z][A-Za-z.'\-]*){1,3}")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z.'\-]*")


def _looks_like_name(line: str) -> bool:
    low = line.lower().strip()
    if low in _NOT_NAME_LINES:
        return False
    if any(w in _ROLE_WORDS for w in re.findall(r"[a-z]+", low)):
        return False
    return bool(_NAME_LINE_RE.fullmatch(line)) and 2 <= len(line.split()) <= 4


def _name_from_filename(filename: str) -> str:
    stem = re.sub(r"[_\-\.]+", " ", Path(filename).stem)
    words = [w for w in stem.split() if w.lower() not in {"resume", "cv", "curriculum", "vitae", "final", "new"}]
    return " ".join(words).title() or "Candidate"


def _guess_name(text: str, filename: str) -> str:
    """
    Best-effort candidate name from the first lines of the resume text.

    1. First line of 2-4 alphabetic words that is not a section heading or job title.
    2. Two (or three) consecutive one-word lines, for PDFs whose extractor splits
       a large name across lines ("Arooj" / "Fatima").
    3. The file name, minus words like "resume" and "cv".
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()][:12]
    log.info("Name guess for %s: first lines=%r", filename, lines[:6])

    for line in lines:
        if _looks_like_name(line):
            return line.title()

    for i in range(len(lines) - 1):
        for size in (2, 3):
            chunk = lines[i:i + size]
            if len(chunk) == size and all(_WORD_RE.fullmatch(w) for w in chunk):
                joined = " ".join(chunk)
                if _looks_like_name(joined):
                    return joined.title()

    return _name_from_filename(filename)


async def _read_upload(file: UploadFile) -> bytes:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTS:
        raise HTTPException(400, f"{file.filename}: unsupported type (use pdf, docx or txt)")
    data = await file.read()
    if not data:
        raise HTTPException(400, f"{file.filename}: file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"{file.filename}: larger than 10 MB")
    return data


def _public(cand: dict) -> dict:
    """Candidate row with the stored JSON blobs parsed for the frontend."""
    out = dict(cand)
    for key in ("screening_json", "evaluation_json"):
        raw = out.pop(key, None)  # state_manager already parsed it; accept both forms
        out[key.replace("_json", "")] = json.loads(raw) if isinstance(raw, str) else (raw or None)
    out.pop("resume_text", None)  # large, and the frontend does not need it
    out["candidate_email"] = out.get("email")
    out["candidate_name"] = out.get("name")
    return out


def _candidate_or_404(email: str) -> dict:
    cand = sm.get_candidate_by_email(email)
    if not cand:
        raise HTTPException(404, f"Unknown candidate: {email}")
    return cand


class RunRequest(BaseModel):
    job_id: Optional[str] = None


class EvaluateRequest(BaseModel):
    interview_notes: str
    job_id: Optional[str] = None


# ==========================================================================
# Routes
# ==========================================================================
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "has_jd": _load_jd() is not None}


@app.post("/jobs")
async def upload_job(file: UploadFile = File(...)) -> dict:
    data = await _read_upload(file)
    text = await asyncio.to_thread(_extract_text, file.filename or "jd.txt", data)
    if not text.strip():
        raise HTTPException(422, "Could not extract any text from the job description")
    job_id = _save_jd(text)
    title = Path(file.filename or "Job").stem.replace("_", " ").strip() or "Job"
    sm.create_job(title=title, jd_text=text, filename=file.filename)
    return {"job_id": job_id, "filename": file.filename, "chars": len(text)}


@app.post("/candidates")
async def upload_resumes(
    files: List[UploadFile] = File(...),
    email: Optional[str] = Form(None),
) -> dict:
    """
    Stage 1 — Ingestion: parse -> SQLite row (PENDING_SCREENING) -> chunk/embed
    -> upsert to Qdrant. `email` is only honoured when exactly one file is sent;
    otherwise the email is read from each resume.
    """
    results = []
    for upload in files:
        name = upload.filename or "resume"
        try:
            data = await _read_upload(upload)
            text = await asyncio.to_thread(_extract_text, name, data)
            if not text.strip():
                raise ValueError("no extractable text (scanned PDF?)")

            cand_email = (email.strip().lower() if email and len(files) == 1 else None) or _find_email(text)
            if not cand_email:
                raise ValueError("no email address found in the resume; send it in the `email` field")

            if sm.get_candidate_by_email(cand_email):
                results.append({"filename": name, "ok": True, "email": cand_email,
                                "note": "already ingested, skipped"})
                continue

            cand_name = _guess_name(text, name)
            cand_id, job_id = _create_candidate(cand_email, cand_name, name, text)
            chunks = await asyncio.to_thread(_index_resume, cand_id, job_id, cand_email, text, name)
            results.append({"filename": name, "ok": True, "email": cand_email,
                            "name": cand_name, "chunks_indexed": chunks,
                            "status": orch.Status.PENDING_SCREENING})
        except HTTPException as exc:
            results.append({"filename": name, "ok": False, "error": exc.detail})
        except Exception as exc:
            log.exception("Ingestion failed for %s", name)
            results.append({"filename": name, "ok": False, "error": str(exc)})
    return {"results": results}


@app.get("/candidates")
def list_candidates(status: Optional[str] = None) -> dict:
    return {"candidates": [_public(c) for c in sm.list_candidates(status=status)]}


@app.get("/candidates/{email}")
def get_candidate(email: str) -> dict:
    return _public(_candidate_or_404(email))


@app.post("/candidates/{email}/run")
def run_candidate(email: str, body: RunRequest = RunRequest()) -> dict:
    cand = _candidate_or_404(email)
    jd = _load_jd(body.job_id)
    if cand["status"] == orch.Status.PENDING_SCREENING and not jd:
        raise HTTPException(400, "Upload a job description first (POST /jobs)")
    addr = email.lower()
    run_id = _launch("candidate", lambda cb: [orch.process_candidate(addr, jd or "", cb)])
    return {"run_id": run_id, "events_url": f"/runs/{run_id}/events"}


@app.post("/pipeline/run")
def run_pipeline(body: RunRequest = RunRequest()) -> dict:
    jd = _load_jd(body.job_id)
    if not jd:
        raise HTTPException(400, "Upload a job description first (POST /jobs)")
    pending = [_email_of(c) for c in sm.list_candidates(status=orch.Status.PENDING_SCREENING)]
    if not pending:
        raise HTTPException(409, "No candidates are waiting for screening")

    def work(cb):
        return [orch.process_candidate(e, jd, cb) for e in pending]

    run_id = _launch("pipeline", work)
    return {"run_id": run_id, "candidates": pending, "events_url": f"/runs/{run_id}/events"}


@app.post("/poll")
def poll() -> dict:
    """Gmail poll: reschedule detection first, then Scheduling Agent routing."""
    run_id = _launch("poll", lambda cb: orch.poll_and_route(cb))
    return {"run_id": run_id, "events_url": f"/runs/{run_id}/events"}


@app.post("/candidates/{email}/evaluate")
def evaluate(email: str, body: EvaluateRequest) -> dict:
    cand = _candidate_or_404(email)
    if not cand.get("screening_json"):
        raise HTTPException(409, "Candidate has not been screened yet")
    if not body.interview_notes.strip():
        raise HTTPException(400, "interview_notes is empty")
    jd = _load_jd(body.job_id)
    if not jd:
        raise HTTPException(400, "Upload a job description first (POST /jobs)")
    addr = email.lower()
    run_id = _launch("evaluation", lambda cb: orch.run_evaluation(addr, jd, body.interview_notes, cb))
    return {"run_id": run_id, "events_url": f"/runs/{run_id}/events"}



# ==========================================================================
# Demo mode (sample data for hackathon judges). Mock mode only: the sample
# candidates use @example.com addresses, so real mode would bounce emails.
# ==========================================================================
# Each simulated reply asks for a different time on the same day, so two
# shortlisted sample candidates never fight for the same calendar slot.
_DEMO_REPLY_TIMES = ["2:00 PM", "3:00 PM", "4:00 PM", "5:00 PM"]


def _demo_enabled() -> bool:
    return bool(config.settings.google_mock_mode)


def _require_demo() -> None:
    if not _demo_enabled():
        raise HTTPException(
            403,
            "Sample data runs in demo mode only. Set GOOGLE_MOCK_MODE=true (no real emails are sent in demo mode).",
        )


def _clear_demo_calendar() -> int:
    """Delete mock calendar events whose attendees are sample (demo) candidates.
    Other events (for example a real test candidate's) are left alone."""
    from tools.google_calendar import _mock_cal

    demo = {e.lower() for e in demo_data.DEMO_EMAILS}

    def drop(data: dict) -> int:
        events = data.get("events", [])
        keep = [
            ev for ev in events
            if not ({str(a).lower() for a in ev.get("attendees", [])} & demo)
        ]
        data["events"] = keep
        return len(events) - len(keep)

    result = _mock_cal.update(drop)
    return result if isinstance(result, int) else 0


@app.get("/demo/status")
def demo_status() -> dict:
    loaded = [e for e in demo_data.DEMO_EMAILS if sm.get_candidate_by_email(e)]
    return {
        "available": _demo_enabled(),
        "loaded": len(loaded) == len(demo_data.DEMO_EMAILS),
        "candidates": len(demo_data.DEMO_RESUMES),
    }


@app.post("/demo/load")
async def demo_load() -> dict:
    """Load the sample job description and sample candidates (no files needed)."""
    _require_demo()
    missing = [r for r in demo_data.DEMO_RESUMES if not sm.get_candidate_by_email(r["email"])]
    if not missing:
        return {"ok": True, "added": 0, "note": "Sample candidates are already loaded."}
    jd = demo_data.DEMO_JOB_DESCRIPTION
    _save_jd(jd)  # newest JD file wins when the pipeline runs
    sm.create_job(title=demo_data.DEMO_JOB_TITLE, jd_text=jd, filename="sample_job_description.txt")
    added = []
    for r in missing:
        cand_id, job_id = _create_candidate(r["email"], r["name"], r["filename"], r["text"])
        await asyncio.to_thread(_index_resume, cand_id, job_id, r["email"], r["text"], r["filename"])
        added.append(r["email"])
    return {"ok": True, "added": len(added), "emails": added}


@app.post("/demo/reply")
def demo_reply() -> dict:
    """Simulate every emailed candidate replying with a specific interview time."""
    _require_demo()
    from tools.google_gmail import mock_inject_reply

    waiting = sm.list_candidates(status="EMAILED_PENDING_REPLY")
    if not waiting:
        raise HTTPException(409, "No candidate is waiting for a reply. Run 'Screen and email candidates' first.")
    base_body = demo_data.demo_reply_text()
    replied = []
    for cand in waiting:
        addr = _email_of(cand)
        # Give each candidate their own hour (2 PM, 3 PM, ...) to avoid slot clashes.
        hour = _DEMO_REPLY_TIMES[len(replied) % len(_DEMO_REPLY_TIMES)]
        body = base_body.replace("2:00 PM", hour)
        try:
            mock_inject_reply(addr, body)
            replied.append(addr)
        except Exception as exc:  # e.g. a candidate emailed in real mode has no mock thread
            log.warning("Could not simulate a reply for %s: %s", addr, exc)
    if not replied:
        raise HTTPException(409, "No candidate has a demo email thread to reply to.")
    return {"ok": True, "replied": replied, "message": base_body}


@app.post("/demo/reset")
def demo_reset() -> dict:
    """Remove the sample candidates, their mock emails and their mock calendar
    events so the demo can be run again from the start."""
    _require_demo()
    removed = 0
    with sm.get_conn() as conn:
        for email in demo_data.DEMO_EMAILS:
            removed += conn.execute("DELETE FROM candidates WHERE email = ?", (email,)).rowcount
    from tools.google_gmail import _mock_mail

    demo = {e.lower() for e in demo_data.DEMO_EMAILS}

    def drop(data: dict) -> None:
        data["messages"] = [
            m for m in data["messages"]
            if m.get("to", "").lower() not in demo and m.get("from", "").lower() not in demo
        ]

    _mock_mail.update(drop)
    events_removed = _clear_demo_calendar()
    return {"ok": True, "removed": removed, "calendar_events_removed": events_removed}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(404, "Unknown run")
    return run


def _sse(data: dict, event: Optional[str] = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {json.dumps(data)}\n\n"


_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}


@app.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> StreamingResponse:
    """Live agent-handoff feed for the frontend visualizer (EventSource-friendly)."""
    if RUNS.get(run_id) is None:
        raise HTTPException(404, "Unknown run")

    async def stream():
        sent = 0
        while True:
            snap = RUNS.snapshot(run_id, sent)
            if snap is None:
                break
            events, done = snap
            for event in events:
                yield _sse(event)
            sent += len(events)
            if done and not events:
                yield _sse({"run_id": run_id}, event="end")
                break
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.3)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


@app.get("/candidates/{email}/evaluation/stream")
async def stream_evaluation(email: str, request: Request, delay: float = 0.04) -> StreamingResponse:
    """Streams the Evaluator's summary word by word (Framer Motion typewriter)."""
    cand = _candidate_or_404(email)
    raw = cand.get("evaluation_json")
    if not raw:
        raise HTTPException(404, "No evaluation yet for this candidate")
    evaluation = json.loads(raw) if isinstance(raw, str) else raw
    delay = min(max(delay, 0.0), 0.5)

    async def stream():
        yield _sse({"recommendation": evaluation.get("recommendation"),
                    "confidence": evaluation.get("confidence")}, event="meta")
        for word in (evaluation.get("summary") or "").split():
            if await request.is_disconnected():
                return
            yield _sse({"word": word})
            await asyncio.sleep(delay)
        yield _sse({"strengths": evaluation.get("strengths", []),
                    "concerns": evaluation.get("concerns", [])}, event="end")

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)