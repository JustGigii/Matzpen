from personal_agent.integrations.telegram.smoke import validated_test_messages


def test_telegram_smoke_messages_preserve_hebrew_without_replacement_characters() -> None:
    messages = validated_test_messages()

    assert len(messages) == 3
    assert all("?" not in message and "\ufffd" not in message for message in messages)
    assert all(
        any("\u0590" <= character <= "\u05ff" for character in message) for message in messages
    )
