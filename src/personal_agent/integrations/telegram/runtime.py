import logging
import uuid
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from personal_agent.core.time import utc_now
from personal_agent.domain.enums import ApprovalStatus, EventDirection, EventSource
from personal_agent.domain.models import (
    ApprovalRequest,
    Commitment,
    Event,
    MemoryFact,
    Reminder,
    Task,
)
from personal_agent.domain.schemas import NormalizedEvent
from personal_agent.integrations.telegram.presentation import (
    approval_card,
    clarification_card,
    pending_action_card,
    reminder_card,
    timetable_card,
)
from personal_agent.services.control import AgentControl
from personal_agent.services.media import MediaAttachment, MediaValidationError

if TYPE_CHECKING:
    from personal_agent.services.briefs import MorningBriefService
    from personal_agent.services.calendar import CalendarService
    from personal_agent.services.confirmations import ConfirmationService
    from personal_agent.services.intake import IntakeService
    from personal_agent.services.media import MediaTextService
    from personal_agent.services.reminders import ReminderService


SpokenIntent = Literal["today", "calendar", "tasks", "commitments", "status", "help"]
logger = logging.getLogger(__name__)
NO_ACTION_MESSAGE = (
    'לא מצאתי בהודעה פעולה שאפשר לבצע. אפשר לנסח אותה כבקשה, למשל: "קבע פגישה עם יואל מחר ב־17:00".'
)


def classify_spoken_intent(text: str) -> SpokenIntent | None:
    """Recognize small, safe Hebrew questions without sending them to the extraction model."""
    normalized = " ".join(text.casefold().split()).strip("?!., ")
    phrases: tuple[tuple[SpokenIntent, tuple[str, ...]], ...] = (
        (
            "today",
            (
                "מה יש היום",
                "מה יש לי היום",
                "מה קורה היום",
                "מה התוכנית היום",
                "תגיד לי מה יש היום",
                "סדר היום",
                "היום שלי",
            ),
        ),
        (
            "calendar",
            (
                "מה ביומן",
                "מה ביומן היום",
                "מה יש ביומן",
                "מה יש לי ביומן",
                "היומן שלי",
                "פגישות היום",
            ),
        ),
        (
            "tasks",
            (
                "מה המשימות",
                "מה המשימות שלי",
                "מה יש לי לעשות",
                "מה אני צריך לעשות",
                "מה צריך לעשות",
            ),
        ),
        ("commitments", ("מה ההתחייבויות", "מה פתוח", "מה הבטחתי", "מה נשאר")),
        ("status", ("מה המצב", "מה הסטטוס", "סטטוס")),
        ("help", ("עזרה", "מה אתה יכול לעשות", "מה אפשר לשאול")),
    )
    for intent, candidates in phrases:
        if normalized in candidates:
            return intent
    return None


def is_authorized_user(user_id: int | None, allowed_user_ids: Collection[int]) -> bool:
    return user_id is not None and user_id in allowed_user_ids


class TelegramRuntime:
    """Authorized Telegram polling runtime and notification adapter."""

    def __init__(
        self,
        token: str,
        allowed_user_ids: tuple[int, ...],
        session_factory: async_sessionmaker[AsyncSession],
        control: AgentControl,
        timezone: str = "Asia/Jerusalem",
        max_media_bytes: int = 18_000_000,
    ) -> None:
        if not token or not allowed_user_ids:
            raise ValueError("Telegram token and at least one allowed user ID are required")
        self._allowed_user_ids = frozenset(allowed_user_ids)
        self._primary_user_id = allowed_user_ids[0]
        self._session_factory = session_factory
        self._control = control
        self._timezone = ZoneInfo(timezone)
        self._max_media_bytes = max_media_bytes
        self._intake_service: IntakeService | None = None
        self._reminder_service: ReminderService | None = None
        self._calendar_service: CalendarService | None = None
        self._confirmation_service: ConfirmationService | None = None
        self._morning_brief_service: MorningBriefService | None = None
        self._media_text_service: MediaTextService | None = None
        self._application = Application.builder().token(token).build()
        self._application.add_handler(CommandHandler("start", self._start_command))
        self._application.add_handler(CommandHandler("status", self._status_command))
        self._application.add_handler(CommandHandler("today", self._today_command))
        self._application.add_handler(CommandHandler("calendar", self._calendar_command))
        self._application.add_handler(CommandHandler("tasks", self._tasks_command))
        self._application.add_handler(CommandHandler("commitments", self._commitments_command))
        self._application.add_handler(CommandHandler("memory", self._memory_command))
        self._application.add_handler(CommandHandler("pause", self._pause_command))
        self._application.add_handler(CommandHandler("resume", self._resume_command))
        self._application.add_handler(CommandHandler("help", self._help_command))
        self._application.add_handler(CommandHandler("reschedule", self._reschedule_command))
        self._application.add_handler(CommandHandler("resolve_time", self._resolve_time_command))
        self._application.add_handler(CallbackQueryHandler(self._callback))
        self._application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._ingest_text)
        )
        self._application.add_handler(
            MessageHandler(
                filters.VOICE | filters.AUDIO | filters.Document.ALL | filters.PHOTO,
                self._ingest_media,
            )
        )

    def bind_services(
        self,
        intake_service: "IntakeService",
        reminder_service: "ReminderService",
        calendar_service: "CalendarService | None" = None,
        confirmation_service: "ConfirmationService | None" = None,
        morning_brief_service: "MorningBriefService | None" = None,
        media_text_service: "MediaTextService | None" = None,
    ) -> None:
        self._intake_service = intake_service
        self._reminder_service = reminder_service
        self._calendar_service = calendar_service
        self._confirmation_service = confirmation_service
        self._morning_brief_service = morning_brief_service
        self._media_text_service = media_text_service

    async def start(self) -> None:
        await self._application.initialize()
        await self._application.start()
        updater = self._application.updater
        if updater is None:
            raise RuntimeError("Telegram updater was not created")
        await updater.start_polling(allowed_updates=Update.ALL_TYPES)

    async def stop(self) -> None:
        updater = self._application.updater
        if updater is not None and updater.running:
            await updater.stop()
        if self._application.running:
            await self._application.stop()
        await self._application.shutdown()

    async def verify_connection(self) -> str:
        bot = await self._application.bot.get_me()
        return bot.username or str(bot.id)

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
        message = await self._application.bot.send_message(
            chat_id=self._primary_user_id,
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
        message = await self._application.bot.send_message(
            chat_id=self._primary_user_id,
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
                    InlineKeyboardButton(
                        "🧠 דחייה חכמה",
                        callback_data=f"smart:{reminder_id}",
                    ),
                ],
                [
                    InlineKeyboardButton("🕓 בחר שעה", callback_data=f"choose:{commitment_id}"),
                    InlineKeyboardButton("🗑️ בטל", callback_data=f"drop:{commitment_id}"),
                ],
                [
                    InlineKeyboardButton("➕ 10 דקות", callback_data=f"quick:{reminder_id}:10"),
                    InlineKeyboardButton("➕ שעה", callback_data=f"quick:{reminder_id}:60"),
                ],
            ]
        )
        message = await self._application.bot.send_message(
            chat_id=self._primary_user_id,
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
        message = await self._application.bot.send_message(
            chat_id=self._primary_user_id,
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
        message = await self._application.bot.send_message(
            chat_id=self._primary_user_id,
            text=clarification_card(summary),
            reply_markup=keyboard,
        )
        return str(message.message_id)

    async def workflow_confirmation(self, message_id: str | None, text: str) -> str:
        if message_id is not None:
            await self._application.bot.edit_message_text(
                chat_id=self._primary_user_id,
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
        message = await self._application.bot.send_message(
            chat_id=self._primary_user_id,
            text=timetable_card(summary),
            reply_markup=keyboard,
        )
        return str(message.message_id)

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        return is_authorized_user(user.id if user is not None else None, self._allowed_user_ids)

    async def _deny(self, update: Update) -> None:
        if update.callback_query is not None:
            await update.callback_query.answer("אין הרשאה", show_alert=True)
        elif update.message is not None:
            await update.message.reply_text("אין הרשאה להשתמש בסוכן הזה.")

    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is not None:
            await update.message.reply_text(
                "הסוכן מחובר. שלח טקסט לניתוח או השתמש ב־/help להצגת הפקודות."
            )

    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is not None:
            await update.message.reply_text(await self._status_text())

    async def _today_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is None:
            return
        if self._morning_brief_service is None:
            await update.message.reply_text("שירות סיכום היום עדיין לא מוכן.")
            return
        brief = await self._morning_brief_service.trigger("telegram_today", force=True, send=False)
        await update.message.reply_text(brief.content)

    async def _tasks_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        await self._render_rows(update, Task, "משימות", "title")

    async def _calendar_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is None:
            return
        if self._calendar_service is None:
            await update.message.reply_text("Google Calendar עדיין לא מוגדר.")
            return
        args = context.args or []
        if len(args) < 3:
            await update.message.reply_text(
                "שימוש: /calendar <start-ISO> <end-ISO> <summary>\n"
                "דוגמה: /calendar 2026-08-01T10:00+03:00 "
                "2026-08-01T11:00+03:00 פגישה"
            )
            return
        try:
            start = datetime.fromisoformat(args[0])
            end = datetime.fromisoformat(args[1])
            summary = " ".join(args[2:])
            approval_id = await self._calendar_service.propose_event(summary, start, end)
        except ValueError as exc:
            await update.message.reply_text(f"בקשה לא תקינה: {exc}")
            return
        await update.message.reply_text(f"נוצרה בקשת אישור: {approval_id}")

    async def _commitments_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        await self._render_rows(update, Commitment, "התחייבויות", "summary")

    async def _memory_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        await self._render_rows(update, MemoryFact, "זיכרונות", "subject")

    async def _render_rows(
        self,
        update: Update,
        model: type[Task] | type[Commitment] | type[MemoryFact],
        title: str,
        field: str,
    ) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is None:
            return
        async with self._session_factory() as session:
            rows = list((await session.scalars(select(model).limit(20))).all())
        values = [f"• {getattr(row, field)}" for row in rows]
        await update.message.reply_text(f"{title}:\n" + ("\n".join(values) if values else "אין"))

    async def _pause_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update):
            await self._deny(update)
            return
        self._control.pause()
        if update.message is not None:
            await update.message.reply_text("הסוכן הושהה.")

    async def _resume_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update):
            await self._deny(update)
            return
        self._control.resume()
        if update.message is not None:
            await update.message.reply_text("הסוכן חזר לפעולה.")

    async def _help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is not None:
            await update.message.reply_text(self._help_text())

    async def _reschedule_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is None or self._reminder_service is None:
            return
        args = context.args or []
        try:
            commitment_id = uuid.UUID(args[0])
            due_at = datetime.fromisoformat(args[1])
        except (ValueError, IndexError):
            await update.message.reply_text("שימוש: /reschedule <commitment-id> <ISO-time>")
            return
        handled = await self._reminder_service.reschedule(commitment_id, due_at)
        await update.message.reply_text("השעה עודכנה." if handled else "הפריט כבר טופל או לא נמצא.")

    async def _resolve_time_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is None or self._confirmation_service is None:
            return
        args = context.args or []
        try:
            approval_id = uuid.UUID(args[0])
            due_at = datetime.fromisoformat(args[1])
        except (ValueError, IndexError):
            await update.message.reply_text("שימוש: /resolve_time <approval-id> <ISO-time>")
            return
        handled = await self._confirmation_service.resolve_time(approval_id, due_at)
        await update.message.reply_text("השעה נשמרה." if handled else "הבקשה כבר טופלה או פגה.")

    async def _callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        query = update.callback_query
        if query is None:
            return
        if not self._authorized(update):
            await self._deny(update)
            return
        data = query.data if isinstance(query.data, str) else ""
        parts = data.split(":")
        try:
            action = parts[0]
            target_id = uuid.UUID(parts[1])
        except (ValueError, AttributeError, IndexError):
            await query.answer("פעולה לא תקינה", show_alert=True)
            return
        handled = False
        if action == "cancel" and self._reminder_service is not None:
            handled = await self._reminder_service.cancel_pending_action(target_id)
        elif action == "execute" and self._reminder_service is not None:
            handled = await self._reminder_service.execute_pending_action_now(target_id)
        elif action in {"approve", "reject"}:
            if self._calendar_service is not None:
                handled = await self._calendar_service.resolve_proposal(
                    target_id, approve=action == "approve"
                )
            if not handled and self._confirmation_service is not None:
                handled = await self._confirmation_service.resolve(
                    target_id, approve=action == "approve"
                )
        elif action in {"confirm", "decline"} and self._confirmation_service is not None:
            handled = await self._confirmation_service.resolve(
                target_id, approve=action == "confirm"
            )
        elif action == "pick" and len(parts) == 3 and self._confirmation_service is not None:
            try:
                hour = int(parts[2])
            except ValueError:
                await query.answer("שעה לא תקינה", show_alert=True)
                return
            handled = await self._confirmation_service.resolve_suggested_hour(
                target_id, hour, str(self._timezone)
            )
        elif action == "done" and self._reminder_service is not None:
            handled = await self._reminder_service.mark_done(target_id)
        elif action == "drop" and self._reminder_service is not None:
            handled = await self._reminder_service.cancel_commitment(target_id)
        elif action == "smart" and self._reminder_service is not None:
            handled = await self._reminder_service.smart_snooze(target_id)
        elif action == "quick" and len(parts) == 3 and self._reminder_service is not None:
            try:
                minutes = int(parts[2])
            except ValueError:
                await query.answer("משך הדחייה אינו תקין", show_alert=True)
                return
            handled = await self._reminder_service.snooze_for(target_id, minutes)
        elif action == "row" and len(parts) == 3 and self._confirmation_service is not None:
            handled = await self._confirmation_service.toggle_timetable_row(
                target_id, int(parts[2])
            )
        elif action == "change":
            await self._show_approval_time_picker(query, target_id)
            return
        elif action == "choose":
            await self._show_reminder_time_picker(query, target_id)
            return
        elif action == "reschedule" and len(parts) == 3 and self._reminder_service is not None:
            try:
                hour = int(parts[2])
            except ValueError:
                await query.answer("שעה לא תקינה", show_alert=True)
                return
            handled = await self._reschedule_for_hour(target_id, hour)
        await query.answer("בוצע" if handled else "הבקשה כבר פגה או טופלה. לא בוצעה פעולה נוספת.")
        if action in {"done", "drop", "smart", "quick", "reschedule"}:
            await self._synchronize_reminder_cards(query, action, target_id, handled, parts)
        elif handled:
            await query.edit_message_text("✅ הפעולה טופלה בהצלחה.")

    async def _synchronize_reminder_cards(
        self,
        query: CallbackQuery,
        action: str,
        target_id: uuid.UUID,
        handled: bool,
        parts: list[str],
    ) -> None:
        item_id = target_id
        async with self._session_factory() as session:
            if action in {"smart", "quick"}:
                source_reminder = await session.get(Reminder, target_id)
                if source_reminder is None:
                    await self._replace_current_callback_card(query, "⌛ הכרטיס הזה כבר אינו פעיל.")
                    return
                item_id = source_reminder.commitment_id or source_reminder.task_id or target_id

            commitment = await session.get(Commitment, item_id)
            task = await session.get(Task, item_id) if commitment is None else None
            summary = (
                commitment.summary if commitment is not None else task.title if task else "הפריט"
            )
            item_status = (
                commitment.status.value
                if commitment is not None
                else task.status.value
                if task is not None
                else None
            )
            reminder_rows = list(
                (
                    await session.scalars(
                        select(Reminder).where(
                            (Reminder.commitment_id == item_id) | (Reminder.task_id == item_id)
                        )
                    )
                ).all()
            )

        if handled:
            title = {
                "done": "✅ סומן כבוצע",
                "drop": "🗑️ התזכורת בוטלה",
                "smart": "🧠 התזכורת נדחתה לזמן מתאים יותר",
                "reschedule": "🕓 המועד עודכן",
            }.get(action, "✅ הפעולה טופלה")
            if action == "quick":
                minutes = int(parts[2])
                title = "⏰ התזכורת נדחתה בשעה" if minutes == 60 else "⏰ התזכורת נדחתה ב־10 דקות"
        elif item_status == "done":
            title = "✅ הפריט כבר סומן כבוצע"
        elif item_status == "cancelled":
            title = "🗑️ הפריט כבר בוטל"
        else:
            title = "⌛ התזכורת הזאת כבר טופלה"

        text = f"{title}\n━━━━━━━━━━━━\n📝 {summary}\n\nהכפתורים בכרטיס הזה אינם פעילים עוד."
        message_ids = {
            reminder.telegram_message_id
            for reminder in reminder_rows
            if reminder.telegram_message_id is not None
        }
        current_message_id = getattr(query.message, "message_id", None)
        if current_message_id is not None:
            message_ids.add(str(current_message_id))

        for message_id in message_ids:
            try:
                await self._application.bot.edit_message_text(
                    chat_id=self._primary_user_id,
                    message_id=int(message_id),
                    text=text,
                )
            except (TelegramError, ValueError) as exc:
                logger.warning(
                    "telegram_reminder_card_update_failed",
                    extra={"message_id": message_id, "error_type": type(exc).__name__},
                )

    async def _replace_current_callback_card(self, query: CallbackQuery, text: str) -> None:
        try:
            await query.edit_message_text(text)
        except TelegramError as exc:
            logger.warning(
                "telegram_callback_card_update_failed",
                extra={"error_type": type(exc).__name__},
            )

    async def _show_approval_time_picker(
        self, query: CallbackQuery, approval_id: uuid.UUID
    ) -> None:
        async with self._session_factory() as session:
            approval = await session.get(ApprovalRequest, approval_id)
        if approval is None or approval.status is not ApprovalStatus.PENDING:
            await query.answer("הבקשה כבר טופלה", show_alert=True)
            await self._replace_current_callback_card(query, "⌛ הבקשה הזאת כבר אינה פעילה.")
            return
        hours = (18, 19, 20)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🕓 {hour:02d}:00", callback_data=f"pick:{approval_id}:{hour}"
                    )
                    for hour in hours
                ],
                [InlineKeyboardButton("🗑️ בטל", callback_data=f"cancel:{approval_id}")],
            ]
        )
        await query.answer()
        await query.edit_message_text(
            "🕓 בחירת שעה חדשה\n━━━━━━━━━━━━\nבחר/י שעה. התאריך המקורי יישמר כשקיים.",
            reply_markup=keyboard,
        )

    async def _show_reminder_time_picker(
        self, query: CallbackQuery, commitment_id: uuid.UUID
    ) -> None:
        async with self._session_factory() as session:
            commitment = await session.get(Commitment, commitment_id)
            task = await session.get(Task, commitment_id) if commitment is None else None
        item_status = (
            commitment.status.value
            if commitment is not None
            else task.status.value
            if task is not None
            else None
        )
        if item_status in {None, "done", "cancelled"}:
            await query.answer("התזכורת כבר טופלה", show_alert=True)
            await self._synchronize_reminder_cards(
                query, "reschedule", commitment_id, False, ["reschedule"]
            )
            return
        hours = (18, 19, 20)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"🕓 {hour:02d}:00", callback_data=f"reschedule:{commitment_id}:{hour}"
                    )
                    for hour in hours
                ],
                [InlineKeyboardButton("🗑️ בטל", callback_data=f"drop:{commitment_id}")],
            ]
        )
        await query.answer()
        await query.edit_message_text(
            "🕓 בחירת שעה חדשה\n━━━━━━━━━━━━\nבחר/י שעה חדשה לתזכורת.",
            reply_markup=keyboard,
        )

    async def _reschedule_for_hour(self, commitment_id: uuid.UUID, hour: int) -> bool:
        if not 0 <= hour <= 23 or self._reminder_service is None:
            return False
        async with self._session_factory() as session:
            commitment = await session.get(Commitment, commitment_id)
            task = await session.get(Task, commitment_id) if commitment is None else None
            if commitment is None and task is None:
                return False
            local_now = utc_now().astimezone(self._timezone)
            if commitment is not None:
                due_at = commitment.due_at
            else:
                assert task is not None
                due_at = task.due_at
            base = due_at.astimezone(self._timezone) if due_at is not None else local_now
        selected = base.replace(hour=hour, minute=0, second=0, microsecond=0)
        if selected <= local_now:
            selected += timedelta(days=1)
        return await self._reminder_service.reschedule(commitment_id, selected)

    async def _status_text(self) -> str:
        async with self._session_factory() as session:
            tasks = await session.scalar(select(func.count()).select_from(Task))
            commitments = await session.scalar(select(func.count()).select_from(Commitment))
            approvals = await session.scalar(select(func.count()).select_from(ApprovalRequest))
        state = "מושהה" if self._control.paused else "פעיל"
        return (
            "🤖 מצב הסוכן\n━━━━━━━━━━━━\n"
            f"🟢 מצב: {state}\n"
            f"☑️ משימות: {tasks}\n"
            f"📌 התחייבויות: {commitments}\n"
            f"✋ בקשות ממתינות: {approvals}"
        )

    @staticmethod
    def _help_text() -> str:
        return (
            "💬 אפשר פשוט לכתוב לי בעברית\n━━━━━━━━━━━━\n"
            "• מה יש היום?\n"
            "• מה ביומן?\n"
            "• מה המשימות שלי?\n"
            "• מה פתוח?\n"
            "• מה המצב?\n\n"
            'אפשר גם לשלוח התחייבות חדשה, למשל: "תזכיר לי להתקשר לדניאל מחר ב־14:15".'
        )

    async def _handle_spoken_request(self, update: Update, text: str | None = None) -> bool:
        message = update.message
        content = text or (message.text if message is not None else None)
        if message is None or not content:
            return False
        intent = classify_spoken_intent(content)
        if intent is None:
            return False
        if intent == "today":
            if self._morning_brief_service is None:
                await message.reply_text("🌤️ סיכום היום עדיין לא מוכן.")
                return True
            brief = await self._morning_brief_service.trigger(
                "telegram_spoken_today", force=True, send=False
            )
            await message.reply_text(brief.content)
            return True
        if intent == "calendar":
            if self._calendar_service is None:
                await message.reply_text("🗓️ Google Calendar עדיין לא מוגדר.")
                return True
            events = await self._calendar_service.list_today()
            lines = ["🗓️ היום ביומן", "━━━━━━━━━━━━"]
            lines.extend(
                f"• {event.start.astimezone(self._timezone):%H:%M} — {event.summary}"
                for event in events
            )
            if not events:
                lines.append("✨ אין אירועים ביומן היום.")
            await message.reply_text("\n".join(lines))
            return True
        if intent == "tasks":
            await self._render_rows(update, Task, "☑️ המשימות שלי", "title")
            return True
        if intent == "commitments":
            await self._render_rows(update, Commitment, "📌 מה פתוח", "summary")
            return True
        if intent == "status":
            await message.reply_text(await self._status_text())
            return True
        await message.reply_text(self._help_text())
        return True

    def _media_descriptor(self, message: Message) -> tuple[str, str, str, str, int | None] | None:
        if message.voice is not None:
            return (
                message.voice.file_id,
                message.voice.file_unique_id,
                "voice.received",
                message.voice.mime_type or "audio/ogg",
                message.voice.file_size,
            )
        if message.audio is not None:
            return (
                message.audio.file_id,
                message.audio.file_unique_id,
                "audio.received",
                message.audio.mime_type or "audio/mpeg",
                message.audio.file_size,
            )
        if message.document is not None:
            return (
                message.document.file_id,
                message.document.file_unique_id,
                "document.received",
                message.document.mime_type or "application/octet-stream",
                message.document.file_size,
            )
        if message.photo:
            photo = message.photo[-1]
            return (
                photo.file_id,
                photo.file_unique_id,
                "photo.received",
                "image/jpeg",
                photo.file_size,
            )
        return None

    async def _ingest_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        message = update.message
        user = update.effective_user
        chat = update.effective_chat
        descriptor = self._media_descriptor(message) if message is not None else None
        if message is None or user is None or chat is None or descriptor is None:
            return
        if self._control.paused:
            await message.reply_text("הסוכן מושהה. השתמש ב־/resume.")
            return
        if self._intake_service is None or self._media_text_service is None:
            await message.reply_text("📎 שירות עיבוד המדיה עדיין לא מוכן.")
            return

        file_id, file_unique_id, event_type, mime_type, declared_size = descriptor
        if declared_size is not None and declared_size > self._max_media_bytes:
            max_megabytes = self._max_media_bytes // 1_000_000
            await message.reply_text(
                f"📎 הקובץ גדול מדי. אפשר לשלוח קובץ בגודל של עד {max_megabytes}MB."
            )
            return
        dedupe_key = f"{chat.id}:{event_type}:{message.message_id}"
        async with self._session_factory() as session:
            existing = await session.scalar(select(Event.id).where(Event.dedupe_key == dedupe_key))
        if existing is not None:
            await message.reply_text("✅ כבר עיבדתי את הקובץ הזה.")
            return

        filename = message.document.file_name if message.document is not None else None
        try:
            telegram_file = await context.bot.get_file(file_id)
            content = bytes(await telegram_file.download_as_bytearray())
            extracted_text = await self._media_text_service.extract_text(
                MediaAttachment(
                    content=content,
                    mime_type=mime_type,
                    filename=filename,
                    file_unique_id=file_unique_id,
                )
            )
        except MediaValidationError as exc:
            logger.info("telegram_media_rejected", extra={"reason": str(exc)})
            await message.reply_text(
                "📎 לא הצלחתי לקרוא את הקובץ. נתמכים: הקלטה, PDF, DOCX, TXT ותמונה."
            )
            return
        except Exception:
            logger.exception("telegram_media_extraction_failed")
            await message.reply_text("⚠️ לא הצלחתי לעבד את הקובץ כרגע. אפשר לנסות שוב מאוחר יותר.")
            return

        if event_type in {"voice.received", "audio.received"} and await self._handle_spoken_request(
            update, extracted_text
        ):
            return

        caption = message.caption.strip() if message.caption else ""
        source_text = f"{caption}\n{extracted_text}".strip()
        result = await self._intake_service.ingest(
            NormalizedEvent(
                source=EventSource.TELEGRAM,
                source_account=str(self._application.bot.id),
                external_id=str(message.message_id),
                event_type=event_type,
                direction=EventDirection.INBOUND,
                occurred_at=message.date,
                received_at=utc_now(),
                actor_external_id=str(user.id),
                actor_display_name=user.full_name,
                conversation_external_id=str(chat.id),
                content_text=source_text,
                payload_json={
                    "message_id": message.message_id,
                    "chat_id": chat.id,
                    "file_unique_id": file_unique_id,
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": len(content),
                },
                dedupe_key=dedupe_key,
            )
        )
        if result.created and not result.approval_ids:
            await message.reply_text(NO_ACTION_MESSAGE)

    async def _ingest_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update):
            await self._deny(update)
            return
        message = update.message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None or not message.text:
            return
        if await self._handle_spoken_request(update):
            return
        if self._control.paused:
            await message.reply_text("הסוכן מושהה. השתמש ב־/resume.")
            return
        if self._intake_service is None:
            await message.reply_text("שירות הקליטה עדיין לא מוכן.")
            return
        result = await self._intake_service.ingest(
            NormalizedEvent(
                source=EventSource.TELEGRAM,
                source_account=str(self._application.bot.id),
                external_id=str(message.message_id),
                event_type="message.received",
                direction=EventDirection.INBOUND,
                occurred_at=message.date,
                received_at=utc_now(),
                actor_external_id=str(user.id),
                actor_display_name=user.full_name,
                conversation_external_id=str(chat.id),
                content_text=message.text,
                payload_json={"message_id": message.message_id, "chat_id": chat.id},
                dedupe_key=f"{chat.id}:message.received:{message.message_id}",
            )
        )
        if result.created and not result.approval_ids:
            await message.reply_text(NO_ACTION_MESSAGE)
