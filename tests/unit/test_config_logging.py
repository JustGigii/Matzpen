from personal_agent.core.config import Settings
from personal_agent.core.logging import REDACTED, redact


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
        telegram_bot_token=None,
        telegram_allowed_user_ids=(),
        shortcut_bearer_token="",
        google_client_secret_file="",
        google_token_file="",
    )

    assert settings.gemini_configured is False
    assert settings.google_calendar_configured is False
    assert settings.shortcut_configured is False
