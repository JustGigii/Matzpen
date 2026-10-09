import logging

import pytest
from pydantic import ValidationError

from personal_agent.core.config import Settings
from personal_agent.core.logging import REDACTED, configure_logging, redact


def test_settings_repr_does_not_expose_webhook_secret() -> None:
    settings = Settings(openwa_webhook_secret="super-secret-value")

    assert "super-secret-value" not in repr(settings)


def test_sensitive_values_are_recursively_redacted() -> None:
    payload = {
        "Authorization": "Bearer secret",
        "nested": {"api_key": "secret", "safe": "visible"},
    }
    assert redact(payload) == {
        "Authorization": REDACTED,
        "nested": {"api_key": REDACTED, "safe": "visible"},
    }


def test_http_transport_info_logs_are_suppressed_to_protect_url_credentials() -> None:
    configure_logging("INFO", json_logs=False)

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING


def test_single_numeric_telegram_user_id_is_normalized() -> None:
    settings = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_ids=123456,
    )

    assert settings.telegram_allowed_user_ids == (123456,)


def test_blank_optional_integration_settings_are_not_configured() -> None:
    settings = Settings(
        gemini_api_key="",
        gemini_model="",
        groq_api_key="",
        cerebras_api_key="",
        mistral_api_key="",
        telegram_bot_token=None,
        telegram_allowed_user_ids=(),
        shortcut_bearer_token="",
        google_client_secret_file="",
        google_token_file="",
    )

    assert settings.gemini_configured is False
    assert settings.groq_configured is False
    assert settings.cerebras_configured is False
    assert settings.mistral_configured is False
    assert settings.google_calendar_configured is False
    assert settings.shortcut_configured is False


def test_oracle_credentials_are_kept_out_of_database_url_and_repr() -> None:
    settings = Settings(
        database_url="oracle+oracledb_async://@",
        oracle_user="ADMIN",
        oracle_password="database-secret",
        oracle_dsn="adb.example:1521/service",
    )

    assert settings.database_connect_args() == {
        "user": "ADMIN",
        "password": "database-secret",
        "dsn": "adb.example:1521/service",
    }
    assert "database-secret" not in repr(settings)
    assert "database-secret" not in settings.database_url


def test_oracle_url_requires_complete_credentials() -> None:
    with pytest.raises(ValidationError, match="ORACLE_USER, ORACLE_PASSWORD and ORACLE_DSN"):
        Settings(
            database_url="oracle+oracledb_async://@",
            oracle_user=None,
            oracle_password=None,
            oracle_dsn=None,
        )
