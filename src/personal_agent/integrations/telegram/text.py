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


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    """Split by UTF-16 units, preserving every character and preferring line breaks."""
    if limit < 2:
        raise ValueError("Telegram text limit must be at least two UTF-16 units")
    chunks: list[str] = []
    while text:
        units = 0
        end = 0
        for character in text:
            units += 2 if ord(character) > 0xFFFF else 1
            if units > limit:
                break
            end += 1
        if end < len(text):
            newline = text.rfind("\n", 0, end)
            if newline >= 0:
                end = newline + 1
        chunks.append(text[:end])
        text = text[end:]
    return chunks or [""]


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
        chunks = split_telegram_text(text)
        for index, chunk in enumerate(chunks):
            message = await self._bot.send_message(
                chat_id=self._user_id,
                text=chunk,
                reply_markup=keyboard if index == len(chunks) - 1 else None,
            )
        return str(message.message_id)

    async def pending_internal_action(
        self, approval_id: str, summary: str, execute_after: datetime
    ) -> str:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ שמור משימה", callback_data=f"execute:{approval_id}"),
                    InlineKeyboardButton("✏️ שנה", callback_data=f"change:{approval_id}"),
                    InlineKeyboardButton("🗑️ אל תשמור", callback_data=f"cancel:{approval_id}"),
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
                    InlineKeyboardButton("✅ סיימתי", callback_data=f"done:{commitment_id}"),
                    InlineKeyboardButton("⏰ לא עכשיו", callback_data=f"smart:{reminder_id}"),
                ],
                [
                    InlineKeyboardButton("🕓 שעה אחרת", callback_data=f"choose:{commitment_id}"),
                    InlineKeyboardButton("🗑️ לא רלוונטי", callback_data=f"drop:{commitment_id}"),
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
                    InlineKeyboardButton("✅ שמור", callback_data=f"approve:{approval_id}"),
                    InlineKeyboardButton("🗑️ אל תשמור", callback_data=f"reject:{approval_id}"),
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

    async def detail_clarification_request(
        self,
        approval_id: str,
        summary: str,
        question: str,
        options: tuple[str, ...],
    ) -> str:
        option_rows = [
            [InlineKeyboardButton(option, callback_data=f"detail:{approval_id}:{index}")]
            for index, option in enumerate(options)
        ]
        keyboard = InlineKeyboardMarkup(
            [
                *option_rows,
                [
                    InlineKeyboardButton(
                        "✏️ אכתוב במילים שלי", callback_data=f"detailother:{approval_id}"
                    ),
                    InlineKeyboardButton("🗑️ בטל", callback_data=f"cancel:{approval_id}"),
                ],
            ]
        )
        message = await self._bot.send_message(
            chat_id=self._user_id,
            text=f"🤔 צריך עוד פרט\n━━━━━━━━━━━━\n{summary}\n\n❓ {question}",
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

    async def group_tracking_request(self, conversation_id: str, display_name: str) -> str:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ עקוב אחרי הקבוצה", callback_data=f"watch:{conversation_id}"
                    ),
                    InlineKeyboardButton("🔕 אל תעקוב", callback_data=f"unwatch:{conversation_id}"),
                ]
            ]
        )
        message = await self._bot.send_message(
            chat_id=self._user_id,
            text=(
                "👥 זוהתה קבוצת WhatsApp חדשה\n"
                "━━━━━━━━━━━━\n"
                f"📛 {display_name}\n\n"
                "לעקוב אחרי תכנון פגישות ועדכונים על התחייבויות בקבוצה?"
            ),
            reply_markup=keyboard,
        )
        return str(message.message_id)

    async def memory_request(self, approval_id: str, summary: str) -> str:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🧠 שמור", callback_data=f"remember:{approval_id}"),
                    InlineKeyboardButton("✖️ אל תשמור", callback_data=f"forget:{approval_id}"),
                ]
            ]
        )
        message = await self._bot.send_message(
            chat_id=self._user_id,
            text=f"🧠 זיכרון מוצע\n━━━━━━━━━━━━\n{summary}",
            reply_markup=keyboard,
        )
        return str(message.message_id)
