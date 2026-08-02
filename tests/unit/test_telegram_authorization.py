from personal_agent.integrations.telegram.runtime import classify_spoken_intent, is_authorized_user


def test_only_configured_telegram_users_are_authorized() -> None:
    allowed = {123, 456}

    assert is_authorized_user(123, allowed) is True
    assert is_authorized_user(999, allowed) is False
    assert is_authorized_user(None, allowed) is False


def test_spoken_hebrew_questions_are_classified_without_llm_extraction() -> None:
    assert classify_spoken_intent("מה יש לי היום?") == "today"
    assert classify_spoken_intent("מה ביומן היום") == "calendar"
    assert classify_spoken_intent("מה המשימות שלי?") == "tasks"
    assert classify_spoken_intent("מה פתוח?") == "commitments"
    assert classify_spoken_intent("מה המצב?") == "status"
    assert classify_spoken_intent("מה אתה יכול לעשות?") == "help"
    assert classify_spoken_intent("תזכיר לי להתקשר לדניאל מחר") is None
    assert classify_spoken_intent("תזכיר לי לשאול את דניאל מה יש היום") is None
