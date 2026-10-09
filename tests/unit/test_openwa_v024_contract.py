from datetime import UTC, datetime

from personal_agent.integrations.openwa.schemas import OpenWAWebhook


def test_v024_message_payload_normalizes_ids_direction_and_media() -> None:
    webhook = OpenWAWebhook.model_validate(
        {
            "event": "message.received",
            "sessionId": "session-1",
            "idempotencyKey": "delivery-key-1",
            "data": {
                "id": "gateway-row-id",
                "waMessageId": "wa-message-id",
                "chatId": "972500000000@c.us",
                "direction": "outgoing",
                "type": "audio",
                "media": {
                    "mimetype": "audio/ogg",
                    "filename": "voice.ogg",
                    "sizeBytes": 123,
                },
            },
        }
    )

    assert webhook.data.message_id == "wa-message-id"
    assert webhook.data.stable_id == "wa-message-id"
    assert webhook.data.from_me is True
    assert webhook.data.message_type == "audio"
    assert webhook.data.media is not None
    assert webhook.data.media.mime_type == "audio/ogg"
    assert webhook.data.media.filename == "voice.ogg"
    assert webhook.data.media.size == 123


def test_v024_unknown_event_is_accepted_and_uses_idempotency_key() -> None:
    webhook = OpenWAWebhook.model_validate(
        {
            "event": "session.reconnect_loop",
            "sessionId": "session-1",
            "idempotencyKey": "stable-redrive-key",
            "deliveryId": "delivery-attempt-2",
            "data": {"status": "reconnecting"},
        }
    )

    assert webhook.stable_event_id(datetime(2026, 10, 9, 20, 0, tzinfo=UTC)) == "stable-redrive-key"
