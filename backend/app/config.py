"""Application configuration loaded from environment variables / .env file."""
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[".env", "../.env"], env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    DATABASE_URL: str = "sqlite:///./conversion_workbench.db"

    # File storage
    UPLOAD_DIR: str = "./uploads"
    OUTPUT_DIR: str = "./outputs"

    # Auth
    JWT_SECRET: str = "trinamix-local-dev-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 480

    # Seed admin
    ADMIN_EMAIL: str = "admin@trinamix.com"
    ADMIN_PASSWORD: str = "admin123"
    ADMIN_NAME: str = "Trinamix Admin"

    # AI provider
    AI_PROVIDER: str = "none"  # none | anthropic | openai
    ANTHROPIC_API_KEY: str = ""
    # Default to Claude Sonnet 4.6 — used by the natural-language rule
    # translator and the AI Copilot. Override via env to pin a different
    # version.
    ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"

    # CORS
    FRONTEND_ORIGIN: str = "http://localhost:5173"

    # Master encryption key used to seal SourceConnection credentials at rest
    # (Fernet, 32-byte url-safe base64). Empty means "auto-generate on first
    # run, persist to MASTER_ENCRYPTION_KEY_FILE, and emit a rotate-in-prod
    # warning". Production deployments MUST set this explicitly and pull it
    # from a secret manager (AWS KMS, Vault, etc.).
    MASTER_ENCRYPTION_KEY: str = ""
    MASTER_ENCRYPTION_KEY_FILE: str = "./.master_key"

    @property
    def upload_path(self) -> Path:
        p = Path(self.UPLOAD_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def output_path(self) -> Path:
        p = Path(self.OUTPUT_DIR).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
