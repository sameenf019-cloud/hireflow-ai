"""Central configuration. All secrets come from environment / .env (never hard-code)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, default))
    return p if p.is_absolute() else (BASE_DIR / p).resolve()


@dataclass(frozen=True)
class Settings:
    # --- Groq / LLM (model is fixed by the project spec) ---
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    groq_model: str = "llama-3.3-70b-versatile"
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.2"))

    # --- Qdrant (remote if QDRANT_URL set, otherwise embedded local storage) ---
    qdrant_url: str = os.getenv("QDRANT_URL", "")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")
    qdrant_local_path: Path = field(default_factory=lambda: _path("QDRANT_LOCAL_PATH", "data/qdrant"))
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "hireflow_resumes")
    dense_model: str = os.getenv("DENSE_MODEL", "BAAI/bge-small-en-v1.5")
    sparse_model: str = os.getenv("SPARSE_MODEL", "Qdrant/bm25")
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "800"))
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "120"))

    # --- SQLite state ---
    sqlite_path: Path = field(default_factory=lambda: _path("SQLITE_PATH", "data/hireflow.db"))
    upload_dir: Path = field(default_factory=lambda: _path("UPLOAD_DIR", "data/uploads"))

    # --- Google (Gmail + Calendar) ---
    google_credentials_file: Path = field(
        default_factory=lambda: _path("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    )
    google_token_file: Path = field(default_factory=lambda: _path("GOOGLE_TOKEN_FILE", "token.json"))
    google_scopes: tuple = (
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar",
    )
    google_mock_mode: bool = _bool("GOOGLE_MOCK_MODE", True)  # True until real creds exist
    recruiter_email: str = os.getenv("RECRUITER_EMAIL", "")
    calendar_id: str = os.getenv("CALENDAR_ID", "primary")
    timezone: str = os.getenv("TIMEZONE", "UTC")
    interview_duration_minutes: int = int(os.getenv("INTERVIEW_DURATION_MINUTES", "30"))

    # --- App ---
    cors_origins: tuple = tuple(
        o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()
    )

    @property
    def crewai_llm_model(self) -> str:
        """LiteLLM-style model string used by CrewAI."""
        return f"groq/{self.groq_model}"

    def ensure_dirs(self) -> None:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        if not self.qdrant_url:
            self.qdrant_local_path.mkdir(parents=True, exist_ok=True)


settings = Settings()


def validate_runtime() -> list[str]:
    """Return a list of human-readable config problems (empty list = OK)."""
    problems = []
    if not settings.groq_api_key:
        problems.append("GROQ_API_KEY is not set.")
    if not settings.google_mock_mode:
        if not settings.google_credentials_file.exists():
            problems.append(f"Google credentials file not found: {settings.google_credentials_file}")
        if not settings.recruiter_email:
            problems.append("RECRUITER_EMAIL is not set.")
    return problems
