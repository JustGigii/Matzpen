from datetime import datetime
from types import TracebackType
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from personal_agent.integrations.telegram.presentation import (
    approval_card,
    clarification_card,
    pending_action_card,
    reminder_card,
    timetable_card,
)


class TelegramTextNotifier:
    """Minimal initialized Telegram client for standalone scripts."""

    def __init__(self, token: str, user_id: int, timezone: str = "Asia/Jerusalem") -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        self._bot = Bot(token)
        self._user_id = user_id
        self._timezone = ZoneInfo(timezone)

    async def __aenter__(self) -> "TelegramTextNotifier":
        await self._bot.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        await self._bot.shutdown()

    async def send_text(self, text: str, approval_id: str | None = None) -> str:
        keyboard = None
        if approval_id is not None:
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✋ אשר", callback_data=f"confirm:{approval_id}"),
                        InlineKeyboardButton("✖️ דחה", callback_data=f"decline:{approval_id}"),
                    ]
                ]
            )
        message = await self._bot.send_message(
            chat_id=self._user_id,
            text=text,
            reply_markup=keyboard,
        )
        return str(message.message_id)

    async def pending_internal_action(
        self, approval_id: str, summary: str, execute_after: datetime
    ) -> str:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ בצע עכשיו", callback_data=f"execute:{approval_id}"),
                    InlineKeyboardButton("✏️ שנה", callback_data=f"change:{approval_id}"),
                    InlineKeyboardButton("🗑️ בטל", callback_data=f"cancel:{approval_id}"),
                ]
            ]
        )
        message = await self._bot.send_message(
            chat_id=self._user_id,
            text=pending_action_card(summary, execute_after, self._timezone),
            reply_markup=keyboard,
        )
        return str(message.message_id)

    async def reminder(
        self,
        commitment_id: str,
        summary: str,
        due_at: datetime,
        reminder_id: str | None = None,
    ) -> str:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ בוצע", callback_data=f"done:{commitment_id}"),
                    InlineKeyboardButton("🧠 דחייה חכמה", callback_data=f"smart:{reminder_id}"),
                ],
                [
                    InlineKeyboardButton("🕓 בחר שעה", callback_data=f"choose:{commitment_id}"),
                    InlineKeyboardButton("🗑️ בטל", callback_data=f"drop:{commitment_id}"),
                ],
            ]
        )
        message = await self._bot.send_message(
            chat_id=self._user_id,
            text=reminder_card(summary, due_at, self._timezone),
            reply_markup=keyboard,
        )
        return str(message.message_id)

    async def approval_request(self, approval_id: str, summary: str) -> str:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ אשר", callback_data=f"approve:{approval_id}"),
                    InlineKeyboardButton("✖️ דחה", callback_data=f"reject:{approval_id}"),
                ]
            ]
        )
        message = await self._bot.send_message(
            chat_id=self._user_id,
            text=approval_card(summary),
            reply_markup=keyboard,
        )
        return str(message.message_id)

    async def clarification_request(
        self, approval_id: str, summary: str, suggested_hours: tuple[int, ...]
    ) -> str:
        choices = [
            InlineKeyboardButton(f"🕓 {hour:02d}:00", callback_data=f"pick:{approval_id}:{hour}")
            for hour in suggested_hours
        ]
        keyboard = InlineKeyboardMarkup(
            [
                choices,
                [
                    InlineKeyboardButton("✏️ שעה אחרת", callback_data=f"change:{approval_id}"),
                    InlineKeyboardButton("🗑️ בטל", callback_data=f"cancel:{approval_id}"),
                ],
            ]
        )
        message = await self._bot.send_message(
            chat_id=self._user_id,
            text=clarification_card(summary),
            reply_markup=keyboard,
        )
        return str(message.message_id)

    async def workflow_confirmation(self, message_id: str | None, text: str) -> str:
        if message_id is not None:
            await self._bot.edit_message_text(
                chat_id=self._user_id,
                message_id=int(message_id),
                text=text,
            )
            return message_id
        return await self.send_text(text)

    async def timetable_request(self, approval_id: str, summary: str, row_count: int) -> str:
        row_buttons = [
            InlineKeyboardButton(f"🔁 שורה {index + 1}", callback_data=f"row:{approval_id}:{index}")
            for index in range(row_count)
        ]
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("📅 הוסף הכל", callback_data=f"confirm:{approval_id}"),
                    InlineKeyboardButton("🗑️ בטל", callback_data=f"cancel:{approval_id}"),
                ],
                *[[button] for button in row_buttons],
            ]
        )
        message = await self._bot.send_message(
            chat_id=self._user_id,
            text=timetable_card(summary),
            reply_markup=keyboard,
        )
        return str(message.message_id)
