from personal_agent.integrations.telegram.runtime import (
    classify_item_resolution,
    classify_spoken_intent,
    is_authorized_user,
)


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


def test_task_navigation_and_resolution_tolerate_common_typos() -> None:
    assert classify_spoken_intent("מה המשיצות שלי") == "tasks"
    assert classify_spoken_intent("מה משימות שלי") == "tasks"
    assert classify_spoken_intent("לבדוק מה המשימות") == "tasks"
    assert classify_spoken_intent("משימה 1 בוצע") == "tasks"
    assert classify_spoken_intent("בוצע סיימתי משימה 1") == "tasks"


def test_single_item_resolution_understands_ordinal_and_title() -> None:
    ordinal = classify_item_resolution("תמחק את השני")
    titled = classify_item_resolution("סיימתי את המשימה לשאול את נועה")

    assert ordinal is not None
    assert ordinal.action == "cancel"
    assert ordinal.ordinal == 2
    assert titled is not None
    assert titled.action == "done"
    assert titled.title_hint == "לשאול את נועה"


def test_single_item_resolution_understands_natural_phrasing() -> None:
    remove_number = classify_item_resolution("תעיף לי את משימה 2")
    irrelevant_number = classify_item_resolution("משימה 3 לא רלוונטית")
    remove_title = classify_item_resolution("תוריד את המשימה של להתקשר לדני")
    done_number = classify_item_resolution("עשיתי את משימה 1")

    assert remove_number is not None and remove_number.action == "cancel"
    assert remove_number.ordinal == 2
    assert irrelevant_number is not None and irrelevant_number.action == "cancel"
    assert irrelevant_number.ordinal == 3
    assert remove_title is not None and remove_title.title_hint == "להתקשר לדני"
    assert done_number is not None and done_number.action == "done"
    assert done_number.ordinal == 1


def test_single_item_resolution_does_not_reverse_negation_or_truncate_numbers() -> None:
    assert classify_item_resolution("אל תמחק את 2") is None
    assert classify_item_resolution("לא סיימתי את 2") is None

    eleven = classify_item_resolution("תמחק את 11")
    twelve = classify_item_resolution("תמחק את 12")
    assert eleven is not None and eleven.ordinal is None
    assert twelve is not None and twelve.ordinal is None


def test_single_item_resolution_preserves_explicit_item_kind() -> None:
    task = classify_item_resolution("תעיף את משימה 2")
    commitment = classify_item_resolution("בטל התחייבות 3")

    assert task is not None and task.item_kind == "task"
    assert commitment is not None and commitment.item_kind == "commitment"
