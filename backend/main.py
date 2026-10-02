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
    return rag_engine.extract_text(filename, data)


def _index_resume(email: str, text: str, filename: str) -> int:
    return rag_engine.index_resume(email, text, filename)


def _create_candidate(email: str, name: Optional[str], filename: str) -> None:
    sm.create_candidate(email, name=name, resume_filename=filename)


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


def _guess_name(text: str, filename: str) -> str:
    """First short alphabetic line of the resume, else the file name."""
    for line in (text or "").splitlines()[:5]:
        line = line.strip()
        if 2 <= len(line.split()) <= 4 and re.fullmatch(r"[A-Za-z.'\- ]+", line):
            return line.title()
    return re.sub(r"[_\-]+", " ", Path(filename).stem).title()


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
        raw = out.pop(key, None)
        out[key.replace("_json", "")] = json.loads(raw) if raw else None
    return out


def _candidate_or_404(email: str) -> dict:
    cand = sm.get_candidate(email.lower())
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

            if sm.get_candidate(cand_email):
                results.append({"filename": name, "ok": True, "email": cand_email,
                                "note": "already ingested, skipped"})
                continue

            cand_name = _guess_name(text, name)
            _create_candidate(cand_email, cand_name, name)
            chunks = await asyncio.to_thread(_index_resume, cand_email, text, name)
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
    evaluation = json.loads(raw)
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
