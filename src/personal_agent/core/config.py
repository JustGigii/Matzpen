from datetime import time
from functools import lru_cache
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_WEBHOOK_SECRET = "development-only-openwa-secret"  # noqa: S105


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    database_url: str = "sqlite+aiosqlite:///./data/personal_agent.db"
    timezone: str = "Asia/Jerusalem"

    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None
    groq_api_key: SecretStr | None = None
    groq_text_model: str = "openai/gpt-oss-120b"
    groq_vision_model: str = "qwen/qwen3.6-27b"
    groq_audio_model: str = "whisper-large-v3"
    cerebras_api_key: SecretStr | None = None
    cerebras_model: str = "gpt-oss-120b"
    mistral_api_key: SecretStr | None = None
    mistral_ocr_model: str = "mistral-ocr-latest"

    oracle_user: str | None = None
    oracle_password: SecretStr | None = None
    oracle_dsn: str | None = None

    telegram_bot_token: SecretStr | None = None
    telegram_allowed_user_ids: tuple[int, ...] = ()
    telegram_max_media_bytes: int = Field(default=18_000_000, ge=1, le=20_000_000)

    openwa_base_url: str = "http://127.0.0.1:2785/api"
    openwa_api_key: SecretStr | None = None
    openwa_session_id: str | None = None
    openwa_webhook_secret: SecretStr = SecretStr(DEVELOPMENT_WEBHOOK_SECRET)
    openwa_signature_header: str = "X-OpenWA-Signature"
    openwa_webhook_max_bytes: int = Field(default=1_048_576, ge=1024, le=10_485_760)
    whatsapp_ignore_archived: bool = True
    whatsapp_ignored_chat_ids: tuple[str, ...] = ()
    whatsapp_archive_refresh_minutes: int = Field(default=30, ge=1, le=1440)
    whatsapp_initial_history_days: int = Field(default=7, ge=1, le=30)
    whatsapp_history_mode: str = "review_only"
    whatsapp_history_max_messages: int = Field(default=3000, ge=1, le=10_000)
    whatsapp_history_max_messages_per_chat: int = Field(default=300, ge=1, le=1000)
    whatsapp_conversation_idle_seconds: int = Field(default=120, ge=1, le=3600)
    whatsapp_urgent_bypass_minutes: int = Field(default=15, ge=1, le=60)
    whatsapp_max_media_bytes: int = Field(default=18_874_368, ge=1, le=20_000_000)
    whatsapp_event_retention_days: int = Field(default=30, ge=1, le=365)
    whatsapp_disconnect_warning_minutes: int = Field(default=10, ge=1, le=1440)
    whatsapp_prompt_new_groups: bool = False

    shortcut_bearer_token: SecretStr | None = None

    google_client_secret_file: str | None = None
    google_token_file: str | None = None
    google_calendar_id: str = "primary"

    internal_action_grace_seconds: int = Field(default=60, ge=0)
    default_reminder_lead_minutes: int = Field(default=15, ge=0)
    proactive_check_interval_seconds: int = Field(default=60, ge=1)
    clarification_fallback_minutes: int = Field(default=10, ge=1)
    approval_expiry_hours: int = Field(default=24, ge=1)
    overdue_grace_minutes: int = Field(default=15, ge=0)
    morning_brief_fallback_time: time = time(10, 0)
    quiet_hours_start: time | None = None
    quiet_hours_end: time | None = None
    log_level: str = "INFO"
    auto_create_schema: bool = False

    @field_validator("telegram_allowed_user_ids", mode="before")
    @classmethod
    def parse_telegram_user_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(int(item.strip()) for item in value.split(",") if item.strip())
        if isinstance(value, int):
            return (value,)
        return value

    @field_validator(
        "gemini_api_key",
        "gemini_model",
        "groq_api_key",
        "cerebras_api_key",
        "mistral_api_key",
        "oracle_user",
        "oracle_password",
        "oracle_dsn",
        "telegram_bot_token",
        "openwa_api_key",
        "openwa_session_id",
        "shortcut_bearer_token",
        "google_client_secret_file",
        "google_token_file",
        "quiet_hours_start",
        "quiet_hours_end",
        mode="before",
    )
    @classmethod
    def empty_strings_are_unconfigured(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("whatsapp_ignored_chat_ids", mode="before")
    @classmethod
    def parse_ignored_chat_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {value}") from exc
        return value

    @field_validator("openwa_signature_header")
    @classmethod
    def validate_signature_header(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("OPENWA_SIGNATURE_HEADER cannot be empty")
        return value

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        webhook_secret = self.openwa_webhook_secret.get_secret_value()
        if not webhook_secret:
            raise ValueError("OPENWA_WEBHOOK_SECRET cannot be empty")
        if self.app_env.lower() == "production" and webhook_secret == DEVELOPMENT_WEBHOOK_SECRET:
            raise ValueError("OPENWA_WEBHOOK_SECRET must be configured in production")
        if (self.openwa_api_key is None) != (self.openwa_session_id is None):
            raise ValueError("OPENWA_API_KEY and OPENWA_SESSION_ID must be configured together")
        if self.openwa_configured:
            parsed_openwa_url = urlparse(self.openwa_base_url)
            private_hosts = {"127.0.0.1", "localhost", "::1", "host.docker.internal"}
            private_http = (
                parsed_openwa_url.scheme == "http" and parsed_openwa_url.hostname in private_hosts
            )
            remote_https = parsed_openwa_url.scheme == "https"
            if (
                not (private_http or remote_https)
                or parsed_openwa_url.username is not None
                or parsed_openwa_url.password is not None
                or parsed_openwa_url.query
                or parsed_openwa_url.fragment
            ):
                raise ValueError(
                    "OPENWA_BASE_URL must use private HTTP or authenticated remote HTTPS"
                )
        if self.whatsapp_history_mode != "review_only":
            raise ValueError("WHATSAPP_HISTORY_MODE must remain review_only")
        if (self.telegram_bot_token is None) != (not self.telegram_allowed_user_ids):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USER_IDS must be configured together"
            )
        if (self.gemini_api_key is None) != (self.gemini_model is None):
            raise ValueError("GEMINI_API_KEY and GEMINI_MODEL must be configured together")
        if self.database_url.startswith("oracle+") and not self.oracle_configured:
            raise ValueError(
                "ORACLE_USER, ORACLE_PASSWORD and ORACLE_DSN are required for an Oracle database"
            )
        if (self.google_client_secret_file is None) != (self.google_token_file is None):
            raise ValueError(
                "GOOGLE_CLIENT_SECRET_FILE and GOOGLE_TOKEN_FILE must be configured together"
            )
        return self

    @property
    def telegram_configured(self) -> bool:
        return self.telegram_bot_token is not None and bool(self.telegram_allowed_user_ids)

    @property
    def gemini_configured(self) -> bool:
        return self.gemini_api_key is not None and self.gemini_model is not None

    @property
    def groq_configured(self) -> bool:
        return self.groq_api_key is not None

    @property
    def cerebras_configured(self) -> bool:
        return self.cerebras_api_key is not None

    @property
    def mistral_configured(self) -> bool:
        return self.mistral_api_key is not None

    @property
    def oracle_configured(self) -> bool:
        return (
            self.oracle_user is not None
            and self.oracle_password is not None
            and self.oracle_dsn is not None
        )

    def database_connect_args(self) -> dict[str, str]:
        if not self.database_url.startswith("oracle+"):
            return {}
        if not self.oracle_configured:
            raise ValueError("Oracle database credentials are incomplete")
        assert self.oracle_user is not None
        assert self.oracle_password is not None
        assert self.oracle_dsn is not None
        return {
            "user": self.oracle_user,
            "password": self.oracle_password.get_secret_value(),
            "dsn": self.oracle_dsn,
        }

    @property
    def google_calendar_configured(self) -> bool:
        return self.google_client_secret_file is not None and self.google_token_file is not None

    @property
    def shortcut_configured(self) -> bool:
        return self.shortcut_bearer_token is not None

    @property
    def openwa_configured(self) -> bool:
        return self.openwa_api_key is not None and self.openwa_session_id is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
