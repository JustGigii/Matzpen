import logging
import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
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
from personal_agent.domain.enums import (
    ApprovalStatus,
    CommitmentStatus,
    EventDirection,
    EventSource,
    MemoryStatus,
    ProcessingStatus,
    Sensitivity,
    TaskStatus,
    WhatsAppConversationType,
)
from personal_agent.domain.models import (
    ApprovalRequest,
    CalendarAction,
    Commitment,
    Event,
    MemoryFact,
    Reminder,
    Task,
    WhatsAppConversation,
)
from personal_agent.domain.schemas import NormalizedEvent
from personal_agent.integrations.llm.base import LLMRetryableError
from personal_agent.integrations.telegram.presentation import (
    approval_card,
    clarification_card,
    pending_action_card,
    reminder_card,
    retryable_llm_message,
    timetable_card,
)
from personal_agent.services.control import AgentControl
from personal_agent.services.media import MediaAttachment, MediaValidationError

if TYPE_CHECKING:
    from personal_agent.services.briefs import MorningBriefService
    from personal_agent.services.calendar import CalendarService
    from personal_agent.services.confirmations import ConfirmationService
    from personal_agent.services.conversation import ConversationService
    from personal_agent.services.intake import IntakeService
    from personal_agent.services.media import MediaTextService
    from personal_agent.services.reminders import ReminderService
    from personal_agent.services.whatsapp import WhatsAppService


SpokenIntent = Literal["today", "calendar", "tasks", "commitments", "status", "help"]
logger = logging.getLogger(__name__)
NO_ACTION_MESSAGE = (
    'לא מצאתי בהודעה פעולה שאפשר לבצע. אפשר לנסח אותה כבקשה, למשל: "קבע פגישה עם יואל מחר ב־17:00".'
)


@dataclass(frozen=True)
class SpokenItemResolution:
    action: Literal["done", "cancel"]
    ordinal: int | None = None
    title_hint: str | None = None
    item_kind: Literal["task", "commitment"] | None = None
    all_visible: bool = False


def classify_item_resolution(text: str) -> SpokenItemResolution | None:
    """Recognize a request about existing items; context determines the bounded scope."""
    normalized = " ".join(text.casefold().split()).strip("?!., ")
    if re.search(
        r"\b(?:אל\s+(?:תמחק|תמחוק|תבטל|תסיר|תעיף|תוריד)|"
        r"לא\s+(?:למחוק|לבטל|להסיר|להעיף|להוריד|סיימתי|עשיתי|בוצע))\b",
        normalized,
    ):
        return None
    cancel_match = re.search(
        r"\b(?:תמחק|תמחוק|מחק|למחוק|תבטל|בטל|תסיר|הסר|תעיף|עיף|תוריד|הורד|"
        r"לא\s+רלוונטי(?:ת|ים|ות)?|לא\s+צריך|עזוב|תוותר|וותר)\b",
        normalized,
    )
    done_match = re.search(
        r"\b(?:סיימתי|סיימנו|השלמתי|גמרתי|בוצע|בוצעה|עשיתי|טופל|טופלה|סגור)\b",
        normalized,
    )
    if cancel_match is None and done_match is None:
        return None
    action: Literal["done", "cancel"] = "cancel" if cancel_match is not None else "done"
    item_kind: Literal["task", "commitment"] | None = None
    if re.search(r"\bמשימ\w*", normalized):
        item_kind = "task"
    elif re.search(r"\bהתחייב\w*", normalized):
        item_kind = "commitment"
    if action == "cancel" and re.fullmatch(
        r"(?:(?:תמחק|תמחוק|מחק|תבטל|בטל|תסיר|הסר|תעיף|עיף|תוריד|הורד)\s+"
        r"(?:לי\s+)?(?:את\s+)?(?:הכל|הכול|כולם|כולן|אותם|אותן|"
        r"כל\s+(?:ה)?(?:משימות|התחייבויות|הפריטים))|"
        r"(?:(?:זה|כל\s+זה|הכל|הכול|כולם|כולן|הם|הן)\s+)?"
        r"לא\s+רלוונטי(?:ת|ים|ות)?)(?:\s+(?:לי|בבקשה))?",
        normalized,
    ):
        return SpokenItemResolution(action=action, item_kind=item_kind, all_visible=True)
    ordinal_words = {
        "הראשון": 1,
        "הראשונה": 1,
        "השני": 2,
        "השנייה": 2,
        "השניה": 2,
        "השלישי": 3,
        "השלישית": 3,
        "הרביעי": 4,
        "הרביעית": 4,
        "החמישי": 5,
        "החמישית": 5,
        "השישי": 6,
        "השישית": 6,
        "השביעי": 7,
        "השביעית": 7,
        "השמיני": 8,
        "השמינית": 8,
        "התשיעי": 9,
        "התשיעית": 9,
        "העשירי": 10,
        "העשירית": 10,
    }
    for word, ordinal in ordinal_words.items():
        if word in normalized.split():
            return SpokenItemResolution(action=action, ordinal=ordinal, item_kind=item_kind)
    numeric = re.search(r"(?<!\d)(10|[1-9])(?!\d)", normalized)
    if numeric is not None:
        return SpokenItemResolution(
            action=action,
            ordinal=int(numeric.group(1)),
            item_kind=item_kind,
        )

    match = cancel_match or done_match
    assert match is not None
    before = normalized[: match.start()].strip()
    after = normalized[match.end() :].strip()
    title_hint = after or before
    title_hint = re.sub(
        r"^(?:לי\s+)?(?:את\s+)?(?:המשימה|ההתחייבות|הפריט)?\s*(?:של|על)?\s*",
        "",
        title_hint,
    )
    title_hint = re.sub(r"\s+(?:בבקשה|לי)$", "", title_hint).strip()
    if title_hint in {"זה", "זאת", "אותו", "אותה", "הזה", "הזאת"}:
        title_hint = ""
    return SpokenItemResolution(
        action=action,
        title_hint=title_hint or None,
        item_kind=item_kind,
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

    # Telegram and WhatsApp input often contains small mobile-keyboard typos.  Keep this
    # deterministic router deliberately narrow: it must contain a task-like token and a
    # navigation/resolution cue, so an actual reminder request is still sent to the LLM.
    tokens = normalized.split()
    has_task_token = any(
        max(
            SequenceMatcher(None, token.removeprefix("ה"), candidate).ratio()
            for candidate in ("משימה", "משימות")
        )
        >= 0.72
        for token in tokens
    )
    navigation_cues = {"מה", "איזה", "אילו", "הצג", "תראה", "שלי", "פתוח", "פתוחות"}
    resolution_cues = {"בוצע", "בוצעה", "סיימתי", "סיימנו", "בטל", "ביטול", "מחק"}
    if has_task_token and (
        navigation_cues.intersection(tokens) or resolution_cues.intersection(tokens)
    ):
        return "tasks"
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
        self._conversation_service: ConversationService | None = None
        self._morning_brief_service: MorningBriefService | None = None
        self._media_text_service: MediaTextService | None = None
        self._whatsapp_service: WhatsAppService | None = None
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
        self._application.add_handler(CommandHandler("groups", self._groups_command))
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
        whatsapp_service: "WhatsAppService | None" = None,
        conversation_service: "ConversationService | None" = None,
    ) -> None:
        self._intake_service = intake_service
        self._reminder_service = reminder_service
        self._calendar_service = calendar_service
        self._confirmation_service = confirmation_service
        self._morning_brief_service = morning_brief_service
        self._media_text_service = media_text_service
        self._whatsapp_service = whatsapp_service
        self._conversation_service = conversation_service

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
                    InlineKeyboardButton("✅ שמור משימה", callback_data=f"execute:{approval_id}"),
                    InlineKeyboardButton("✏️ שנה", callback_data=f"change:{approval_id}"),
                    InlineKeyboardButton("🗑️ אל תשמור", callback_data=f"cancel:{approval_id}"),
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
        rows = [
            [
                InlineKeyboardButton("✅ סיימתי", callback_data=f"done:{commitment_id}"),
                InlineKeyboardButton(
                    "⏰ לא עכשיו",
                    callback_data=f"smart:{reminder_id}",
                ),
            ],
            [
                InlineKeyboardButton("🕓 שעה אחרת", callback_data=f"choose:{commitment_id}"),
                InlineKeyboardButton("🗑️ לא רלוונטי", callback_data=f"drop:{commitment_id}"),
            ],
            [
                InlineKeyboardButton("➕ 10 דקות", callback_data=f"quick:{reminder_id}:10"),
                InlineKeyboardButton("➕ שעה", callback_data=f"quick:{reminder_id}:60"),
            ],
        ]
        if getattr(self, "_calendar_service", None) is not None:
            rows.append(
                [InlineKeyboardButton("🗓️ הוסף ליומן", callback_data=f"calendarize:{commitment_id}")]
            )
        keyboard = InlineKeyboardMarkup(rows)
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
                    InlineKeyboardButton("✅ שמור", callback_data=f"approve:{approval_id}"),
                    InlineKeyboardButton("🗑️ אל תשמור", callback_data=f"reject:{approval_id}"),
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

    async def detail_clarification_request(
        self,
        approval_id: str,
        summary: str,
        question: str,
        options: tuple[str, ...],
    ) -> str:
        message = await self._application.bot.send_message(
            chat_id=self._primary_user_id,
            text=f"🤔 צריך עוד פרט\n━━━━━━━━━━━━\n{summary}\n\n❓ {question}",
            reply_markup=self._detail_clarification_keyboard(uuid.UUID(approval_id), options),
        )
        return str(message.message_id)

    @staticmethod
    def _detail_clarification_keyboard(
        approval_id: uuid.UUID, options: tuple[str, ...]
    ) -> InlineKeyboardMarkup:
        option_rows = [
            [InlineKeyboardButton(option, callback_data=f"detail:{approval_id}:{index}")]
            for index, option in enumerate(options)
        ]
        return InlineKeyboardMarkup(
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
        message = await self._application.bot.send_message(
            chat_id=self._primary_user_id,
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
        message = await self._application.bot.send_message(
            chat_id=self._primary_user_id,
            text=f"🧠 זיכרון מוצע\n━━━━━━━━━━━━\n{summary}",
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
        await self._render_task_cards(update)

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
        await self._render_commitment_cards(update)

    async def _render_commitment_cards(self, update: Update) -> None:
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is None:
            return
        async with self._session_factory() as session:
            commitments = list(
                (
                    await session.scalars(
                        select(Commitment)
                        .where(
                            Commitment.status.not_in(
                                [CommitmentStatus.DONE, CommitmentStatus.CANCELLED]
                            )
                        )
                        .order_by(Commitment.due_at.is_(None), Commitment.due_at)
                        .limit(11)
                    )
                ).all()
            )
            cards: list[tuple[Commitment, str]] = []
            for commitment in commitments[:10]:
                source_event = await session.get(Event, commitment.source_event_id)
                calendar_action = await session.scalar(
                    select(CalendarAction).where(CalendarAction.commitment_id == commitment.id)
                )
                cards.append(
                    (
                        commitment,
                        self._commitment_detail(commitment, source_event, calendar_action),
                    )
                )
        if not cards:
            content = "📋 התחייבויות\n━━━━━━━━━━━━\nאין התחייבויות פתוחות."
            await update.message.reply_text(content)
            await self._remember_visible_items(update, "commitment", [], content)
            return
        lines = [
            f"📋 ההתחייבויות הפתוחות ({len(cards)})"
            + (" — מוצגות 10 הקרובות" if len(commitments) > 10 else ""),
            "━━━━━━━━━━━━",
        ]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for index, (commitment, card) in enumerate(cards, start=1):
            lines.append(f"{index}. {' '.join(card.split())[:260]}")
            action_row = [
                InlineKeyboardButton(f"✅ {index}", callback_data=f"done:{commitment.id}"),
                InlineKeyboardButton(f"🗑️ {index}", callback_data=f"drop:{commitment.id}"),
                InlineKeyboardButton(f"🕓 {index}", callback_data=f"choose:{commitment.id}"),
            ]
            if (
                getattr(self, "_calendar_service", None) is not None
                and commitment.due_at is not None
            ):
                action_row.append(
                    InlineKeyboardButton(f"🗓️ {index}", callback_data=f"calendarize:{commitment.id}")
                )
            keyboard_rows.append(action_row)
        lines.append("\nאפשר גם לכתוב: „תעיף את 2” או „סיימתי את 1”.")
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )
        await self._remember_visible_items(
            update, "commitment", [item.id for item, _card in cards], "\n".join(lines)
        )

    def _commitment_detail(
        self,
        commitment: Commitment,
        source_event: Event | None,
        calendar_action: CalendarAction | None,
    ) -> str:
        status_labels = {
            CommitmentStatus.DETECTED: "ממתינה לשמירה",
            CommitmentStatus.SCHEDULED: "מתוזמנת",
            CommitmentStatus.OVERDUE: "באיחור",
        }
        due = (
            commitment.due_at.astimezone(self._timezone).strftime("%d.%m.%Y בשעה %H:%M")
            if commitment.due_at is not None
            else "ללא מועד"
        )
        lines = [
            f"📝 {commitment.summary}",
            f"📅 {due}",
            f"📌 סטטוס: {status_labels.get(commitment.status, commitment.status.value)}",
        ]
        if source_event is not None:
            conversation_name = source_event.payload_json.get("conversation_display_name")
            if isinstance(conversation_name, str) and conversation_name.strip():
                lines.append(f"💬 מקור: WhatsApp עם {conversation_name.strip()}")
            else:
                lines.append(f"💬 מקור: {source_event.source.value}")
        if calendar_action is not None:
            calendar_labels = {
                "executed": "נוסף ל־Google Calendar",
                "pending": "ממתין לאישור Calendar",
                "pending_configuration": "ממתין לחיבור Calendar",
                "failed": "יצירת האירוע נכשלה",
            }
            lines.append(
                "🗓️ "
                + calendar_labels.get(calendar_action.status.value, calendar_action.status.value)
            )
        return "\n".join(lines)

    async def _memory_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is None:
            return
        async with self._session_factory() as session:
            memories = list(
                (
                    await session.scalars(
                        select(MemoryFact)
                        .where(MemoryFact.status == MemoryStatus.CONFIRMED)
                        .order_by(MemoryFact.last_verified_at.desc())
                        .limit(20)
                    )
                ).all()
            )
        if not memories:
            await update.message.reply_text(
                "🧠 זיכרונות\n━━━━━━━━━━━━\nאין עדיין זיכרונות מאושרים."
            )
            return
        lines = [
            f"• {memory.subject} — {memory.predicate}: {memory.value_json.get('value', '')}"
            for memory in memories
        ]
        await update.message.reply_text("🧠 זיכרונות מאושרים\n━━━━━━━━━━━━\n" + "\n".join(lines))

    async def _render_rows(
        self,
        update: Update,
        model: type[Commitment] | type[MemoryFact],
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

    async def _render_task_cards(self, update: Update) -> None:
        """Render open tasks as one compact, actionable Telegram message."""

        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is None:
            return
        async with self._session_factory() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(Task)
                        .where(Task.status.not_in([TaskStatus.DONE, TaskStatus.CANCELLED]))
                        .order_by(Task.due_at.is_(None), Task.due_at, Task.created_at)
                        .limit(11)
                    )
                ).all()
            )
        visible_tasks = tasks[:10]
        if not visible_tasks:
            content = "☑️ המשימות שלי\n━━━━━━━━━━━━\n✨ אין משימות פתוחות."
            await update.message.reply_text(content)
            await self._remember_visible_items(update, "task", [], content)
            return

        suffix = " — מוצגות 10 הקרובות" if len(tasks) > 10 else ""
        lines = [f"☑️ המשימות הפתוחות ({len(visible_tasks)}){suffix}", "━━━━━━━━━━━━"]
        rows: list[list[InlineKeyboardButton]] = []
        for index, task in enumerate(visible_tasks, start=1):
            due_text = (
                f"🕓 {task.due_at.astimezone(self._timezone):%d.%m.%Y בשעה %H:%M}"
                if task.due_at is not None
                else "🕓 ללא מועד"
            )
            description = task.description.strip() if task.description else ""
            details = f"\n   📎 {description[:120]}" if description else ""
            lines.append(f"{index}. {task.title[:160]}\n   {due_text}{details}")
            action_row = [
                InlineKeyboardButton(f"✅ {index}", callback_data=f"done:{task.id}"),
                InlineKeyboardButton(f"🗑️ {index}", callback_data=f"drop:{task.id}"),
                InlineKeyboardButton(f"🕓 {index}", callback_data=f"choose:{task.id}"),
            ]
            if getattr(self, "_calendar_service", None) is not None and task.due_at is not None:
                action_row.append(
                    InlineKeyboardButton(f"🗓️ {index}", callback_data=f"calendarize:{task.id}")
                )
            rows.append(action_row)
        lines.append("\nאפשר גם לכתוב: „תעיף את 2” או „סיימתי את 1”.")
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(rows),
        )
        await self._remember_visible_items(
            update, "task", [task.id for task in visible_tasks], "\n".join(lines)
        )

    async def _remember_visible_items(
        self,
        update: Update,
        kind: Literal["task", "commitment"],
        item_ids: list[uuid.UUID],
        content: str,
    ) -> None:
        """Keep the actual displayed scope and order available across process restarts."""
        chat = getattr(update, "effective_chat", None)
        if chat is None:
            return
        reply_id = uuid.uuid4()
        now = utc_now()
        async with self._session_factory() as session:
            session.add(
                Event(
                    source=EventSource.TELEGRAM,
                    source_account="task-interface",
                    external_id=str(reply_id),
                    event_type="assistant.reply",
                    direction=EventDirection.OUTBOUND,
                    occurred_at=now,
                    received_at=now,
                    actor_external_id="personal-agent",
                    conversation_external_id=str(chat.id),
                    content_text=content,
                    payload_json={
                        "visible_item_kind": kind,
                        "visible_item_ids": [str(item_id) for item_id in item_ids],
                    },
                    dedupe_key=f"visible-items:{reply_id}",
                    sensitivity=Sensitivity.PERSONAL,
                    processing_status=ProcessingStatus.PROCESSED,
                )
            )
            await session.commit()

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

    async def _groups_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if not self._authorized(update):
            await self._deny(update)
            return
        if update.message is None:
            return
        async with self._session_factory() as session:
            groups = list(
                (
                    await session.scalars(
                        select(WhatsAppConversation)
                        .where(
                            WhatsAppConversation.chat_type == WhatsAppConversationType.GROUP,
                            WhatsAppConversation.ignored.is_(False),
                        )
                        .order_by(WhatsAppConversation.display_name)
                    )
                ).all()
            )
        if not groups:
            await update.message.reply_text("לא נמצאו עדיין קבוצות WhatsApp.")
            return
        lines = ["👥 מעקב קבוצות WhatsApp", "━━━━━━━━━━━━"]
        buttons: list[list[InlineKeyboardButton]] = []
        for group in groups:
            label = group.display_name or group.external_chat_id
            status = "פעיל ✅" if group.tracking_enabled else "כבוי 🔕"
            lines.append(f"• {label} — {status}")
            action = "unwatch" if group.tracking_enabled else "watch"
            button_label = f"🔕 הפסק: {label}" if group.tracking_enabled else f"✅ עקוב: {label}"
            buttons.append(
                [
                    InlineKeyboardButton(
                        button_label[:60],
                        callback_data=f"{action}:{group.id}",
                    )
                ]
            )
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

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
        if action in {"watch", "unwatch"} and self._whatsapp_service is not None:
            group_label = await self._whatsapp_service.set_group_tracking(
                target_id,
                enabled=action == "watch",
            )
            if group_label is None:
                await query.answer("הקבוצה לא נמצאה או שכבר אינה זמינה.", show_alert=True)
                return
            state = "הופעל" if action == "watch" else "הופסק"
            await query.answer("בוצע")
            await query.edit_message_text(f"✅ המעקב {state} עבור הקבוצה: {group_label}")
            return
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
        elif action in {"remember", "forget"} and self._conversation_service is not None:
            handled = await self._conversation_service.resolve_memory(
                target_id, remember=action == "remember"
            )
        elif action == "detail" and len(parts) == 3 and self._intake_service is not None:
            async with self._session_factory() as session:
                approval = await session.get(ApprovalRequest, target_id)
            try:
                option_index = int(parts[2])
                item_payload = approval.action_payload["item"] if approval is not None else {}
                _, options = self._intake_service._detail_prompt(item_payload)
                answer = options[option_index]
            except (ValueError, IndexError, KeyError, TypeError):
                await query.answer("האפשרות כבר אינה זמינה", show_alert=True)
                return
            try:
                resolution = await self._intake_service.refine_approval(target_id, answer)
            except LLMRetryableError as exc:
                await query.answer(retryable_llm_message(exc), show_alert=True)
                return
            await self._render_detail_resolution(query, target_id, resolution)
            return
        elif action == "detailother" and self._intake_service is not None:
            await self._request_manual_time(query, target_id, kind="detail")
            return
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
        elif action == "calendarize":
            if self._calendar_service is None:
                await query.answer("Google Calendar עדיין לא מחובר.", show_alert=True)
                return
            try:
                calendar_result = await self._calendar_service.add_item_to_calendar(target_id)
            except Exception:
                logger.exception("telegram_calendarize_item_failed")
                await query.answer("לא הצלחתי להוסיף ליומן. אפשר לנסות שוב.", show_alert=True)
                return
            messages = {
                "created": "נוסף ל־Google Calendar ✅",
                "already_exists": "הפריט כבר נמצא ביומן.",
                "untimed": "צריך קודם לקבוע מועד לפריט.",
                "closed": "הפריט כבר סגור.",
                "missing": "הפריט לא נמצא.",
            }
            await query.answer(
                messages[calendar_result],
                show_alert=calendar_result not in {"created", "already_exists"},
            )
            return
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
        elif action in {"manualapproval", "manualitem"}:
            await self._request_manual_time(
                query,
                target_id,
                kind="approval" if action == "manualapproval" else "item",
            )
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
                "drop": "🗑️ הוסר כלא רלוונטי (לא סומן כבוצע)",
                "smart": "⏰ התזכורת נדחתה לזמן מתאים יותר",
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
        current_text = getattr(query.message, "text", "") or ""
        is_compact_dashboard = current_text.startswith(
            ("☑️ המשימות הפתוחות", "📋 ההתחייבויות הפתוחות")
        )
        if current_message_id is not None and not is_compact_dashboard:
            message_ids.add(str(current_message_id))
        elif is_compact_dashboard:
            markup = getattr(query.message, "reply_markup", None)
            remaining_rows = [
                row
                for row in getattr(markup, "inline_keyboard", [])
                if not any((button.callback_data or "").endswith(f":{item_id}") for button in row)
            ]
            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(remaining_rows) if remaining_rows else None
                )
            except TelegramError as exc:
                logger.warning(
                    "telegram_dashboard_update_failed",
                    extra={"error_type": type(exc).__name__},
                )

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
                [
                    InlineKeyboardButton(
                        "⌨️ כתוב שעה", callback_data=f"manualapproval:{approval_id}"
                    ),
                    InlineKeyboardButton("🗑️ בטל", callback_data=f"cancel:{approval_id}"),
                ],
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
                [
                    InlineKeyboardButton("⌨️ כתוב שעה", callback_data=f"manualitem:{commitment_id}"),
                    InlineKeyboardButton("🗑️ בטל", callback_data=f"drop:{commitment_id}"),
                ],
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

    async def _request_manual_time(
        self,
        query: CallbackQuery,
        target_id: uuid.UUID,
        *,
        kind: Literal["approval", "item", "detail"],
    ) -> None:
        await query.answer()
        is_detail = kind == "detail"
        prompt = await self._application.bot.send_message(
            chat_id=self._primary_user_id,
            text=(
                "✏️ פירוט במילים שלך\n━━━━━━━━━━━━\nכתוב בתגובה להודעה הזאת את הפרט החסר."
                if is_detail
                else "⌨️ כתיבת שעה ידנית\n"
                "━━━━━━━━━━━━\n"
                "כתוב שעה בתגובה להודעה הזאת.\n"
                "דוגמאות: 19:30, מחר 08:15, 04.08 17:45"
            ),
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder=("אפשר לפרט כאן" if is_detail else "לדוגמה: 19:30"),
            ),
        )
        effective_at = utc_now()
        async with self._session_factory() as session:
            session.add(
                Event(
                    source=EventSource.TELEGRAM,
                    source_account=str(self._application.bot.id),
                    external_id=str(prompt.message_id),
                    event_type="manual_time.prompt",
                    direction=EventDirection.OUTBOUND,
                    occurred_at=effective_at,
                    received_at=effective_at,
                    actor_external_id="personal-agent",
                    actor_display_name="Personal Agent",
                    conversation_external_id=str(self._primary_user_id),
                    content_text="Manual time requested",
                    payload_json={"kind": kind, "target_id": str(target_id)},
                    dedupe_key=f"telegram:manual_time_prompt:{prompt.message_id}",
                    sensitivity=Sensitivity.PERSONAL,
                    processing_status=ProcessingStatus.PENDING,
                )
            )
            await session.commit()

    async def _handle_manual_time_reply(self, message: Message, conversation_id: str) -> bool:
        replied_to = getattr(message, "reply_to_message", None)
        prompt_message_id = getattr(replied_to, "message_id", None)
        if prompt_message_id is None or not message.text:
            return False
        async with self._session_factory() as session:
            prompt = await session.scalar(
                select(Event).where(
                    Event.source == EventSource.TELEGRAM,
                    Event.event_type == "manual_time.prompt",
                    Event.external_id == str(prompt_message_id),
                    Event.conversation_external_id == conversation_id,
                    Event.processing_status == ProcessingStatus.PENDING,
                )
            )
            if prompt is None:
                return False
            kind = prompt.payload_json.get("kind")
            try:
                target_id = uuid.UUID(str(prompt.payload_json["target_id"]))
            except (KeyError, ValueError):
                prompt.processing_status = ProcessingStatus.FAILED
                await session.commit()
                return False
            prompt_record_id = prompt.id
            base_date = await self._manual_time_base_date(session, kind, target_id)

        if kind == "detail" and self._intake_service is not None:
            try:
                resolution = await self._intake_service.refine_approval(target_id, message.text)
            except LLMRetryableError as exc:
                await message.reply_text(retryable_llm_message(exc))
                return True
            handled = resolution.state != "inactive"
            async with self._session_factory() as session:
                stored_prompt = await session.get(Event, prompt_record_id)
                if stored_prompt is not None:
                    stored_prompt.processing_status = (
                        ProcessingStatus.PROCESSED if handled else ProcessingStatus.FAILED
                    )
                    stored_prompt.payload_json = {
                        **stored_prompt.payload_json,
                        "response_message_id": message.message_id,
                    }
                    await session.commit()
            await self._reply_detail_resolution(message, target_id, resolution)
            return True

        try:
            selected = self._parse_manual_datetime(message.text, base_date, utc_now())
        except ValueError:
            await message.reply_text(
                "לא הצלחתי להבין את השעה. כתוב למשל `19:30`, `מחר 08:15` או `04.08 17:45`.",
                parse_mode="Markdown",
            )
            return True

        handled = False
        if kind == "approval" and self._confirmation_service is not None:
            handled = await self._confirmation_service.resolve_time(target_id, selected)
        elif kind == "item" and self._reminder_service is not None:
            handled = await self._reminder_service.reschedule(target_id, selected)

        async with self._session_factory() as session:
            stored_prompt = await session.get(Event, prompt_record_id)
            if stored_prompt is not None:
                stored_prompt.processing_status = (
                    ProcessingStatus.PROCESSED if handled else ProcessingStatus.FAILED
                )
                stored_prompt.payload_json = {
                    **stored_prompt.payload_json,
                    "selected_at": selected.isoformat(),
                    "response_message_id": message.message_id,
                }
                await session.commit()
        if handled:
            await message.reply_text(
                f"✅ השעה עודכנה ל־{selected.astimezone(self._timezone):%d.%m.%Y בשעה %H:%M}."
            )
        else:
            await message.reply_text("הבקשה כבר טופלה או שאינה פעילה.")
        return True

    async def _render_detail_resolution(
        self, query: CallbackQuery, approval_id: uuid.UUID, resolution: object
    ) -> None:
        state = getattr(resolution, "state", "inactive")
        summary = getattr(resolution, "summary", "")
        if state == "executed":
            await query.answer("הפרט הושלם וההתחייבות נשמרה")
            return
        if state == "question":
            question = getattr(resolution, "question", None) or "אפשר לפרט עוד?"
            options = tuple(getattr(resolution, "options", ()))
            await query.answer()
            await query.edit_message_text(
                f"🤔 צריך עוד פרט\n━━━━━━━━━━━━\n{summary}\n\n❓ {question}",
                reply_markup=self._detail_clarification_keyboard(approval_id, options),
            )
            return
        if state == "time":
            await self._show_approval_time_picker(query, approval_id)
            return
        if state == "confirmation":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ אשר", callback_data=f"confirm:{approval_id}"),
                        InlineKeyboardButton("✖️ דחה", callback_data=f"decline:{approval_id}"),
                    ]
                ]
            )
            await query.answer()
            await query.edit_message_text(approval_card(summary), reply_markup=keyboard)
            return
        await query.answer("הבקשה כבר טופלה או פגה", show_alert=True)

    async def _reply_detail_resolution(
        self, message: Message, approval_id: uuid.UUID, resolution: object
    ) -> None:
        state = getattr(resolution, "state", "inactive")
        summary = getattr(resolution, "summary", "")
        if state == "executed":
            await message.reply_text("✅ הפרט הושלם וההתחייבות נשמרה.")
        elif state == "question":
            question = getattr(resolution, "question", None) or "אפשר לפרט עוד?"
            options = tuple(getattr(resolution, "options", ()))
            await message.reply_text(
                f"🤔 צריך עוד פרט\n━━━━━━━━━━━━\n{summary}\n\n❓ {question}",
                reply_markup=self._detail_clarification_keyboard(approval_id, options),
            )
        elif state == "time":
            await message.reply_text(
                "🕓 נשאר לבחור שעה.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("בחר שעה", callback_data=f"change:{approval_id}")]]
                ),
            )
        elif state == "confirmation":
            await message.reply_text(
                approval_card(summary),
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("✅ אשר", callback_data=f"confirm:{approval_id}"),
                            InlineKeyboardButton("✖️ דחה", callback_data=f"decline:{approval_id}"),
                        ]
                    ]
                ),
            )
        else:
            await message.reply_text("הבקשה כבר טופלה או פגה.")

    async def _manual_time_base_date(
        self,
        session: AsyncSession,
        kind: object,
        target_id: uuid.UUID,
    ) -> date | None:
        value: object = None
        explicit_date: object = None
        if kind == "approval":
            approval = await session.get(ApprovalRequest, target_id)
            if approval is not None:
                item = approval.action_payload.get("item")
                if isinstance(item, dict):
                    value = item.get("due_at")
                    explicit_date = item.get("explicit_date")
        elif kind == "item":
            commitment = await session.get(Commitment, target_id)
            task = await session.get(Task, target_id) if commitment is None else None
            value = commitment.due_at if commitment is not None else task.due_at if task else None
        if isinstance(value, datetime):
            return value.astimezone(self._timezone).date()
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=self._timezone)
                return parsed.astimezone(self._timezone).date()
            except ValueError:
                pass
        if isinstance(explicit_date, str):
            try:
                return date.fromisoformat(explicit_date)
            except ValueError:
                pass
        return None

    def _parse_manual_datetime(
        self,
        value: str,
        base_date: date | None,
        reference_at: datetime,
    ) -> datetime:
        text = " ".join(value.strip().split())
        local_now = reference_at.astimezone(self._timezone)
        iso_candidate = text.replace(" ", "T", 1)
        if re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{1,2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?", iso_candidate
        ):
            parsed = datetime.fromisoformat(iso_candidate.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=self._timezone) if parsed.tzinfo is None else parsed

        dated = re.fullmatch(
            r"(?P<day>\d{1,2})[./](?P<month>\d{1,2})(?:[./](?P<year>\d{4}))?\s+"
            r"(?P<hour>\d{1,2})[:.](?P<minute>\d{2})",
            text,
        )
        if dated:
            year = int(dated.group("year") or local_now.year)
            selected = datetime(
                year,
                int(dated.group("month")),
                int(dated.group("day")),
                int(dated.group("hour")),
                int(dated.group("minute")),
                tzinfo=self._timezone,
            )
            if dated.group("year") is None and selected <= local_now:
                selected = selected.replace(year=year + 1)
            return selected

        clock = re.fullmatch(
            r"(?:(?P<day_word>היום|מחר)\s+)?(?P<hour>\d{1,2})[:.](?P<minute>\d{2})",
            text,
        )
        if clock is None:
            raise ValueError("Manual time is not recognized")
        hour = int(clock.group("hour"))
        minute = int(clock.group("minute"))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Manual time is outside the valid clock range")
        day_word = clock.group("day_word")
        selected_date = (
            local_now.date() + timedelta(days=1)
            if day_word == "מחר"
            else local_now.date()
            if day_word == "היום"
            else base_date or local_now.date()
        )
        selected = datetime.combine(selected_date, time(hour, minute), self._timezone)
        if day_word is None and selected <= local_now:
            selected += timedelta(days=1)
        return selected

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
            "👥 /groups — ניהול מעקב אחרי קבוצות WhatsApp\n\n"
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
        item_resolution = classify_item_resolution(content)
        if item_resolution is not None:
            await self._resolve_spoken_item(update, item_resolution)
            return True
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
            await self._render_task_cards(update)
            return True
        if intent == "commitments":
            await self._render_commitment_cards(update)
            return True
        if intent == "status":
            await message.reply_text(await self._status_text())
            return True
        await message.reply_text(self._help_text())
        return True

    async def _resolve_spoken_item(self, update: Update, resolution: SpokenItemResolution) -> None:
        message = update.message
        chat = update.effective_chat
        if message is None or chat is None or self._reminder_service is None:
            return
        async with self._session_factory() as session:
            commitments = list(
                (
                    await session.scalars(
                        select(Commitment)
                        .where(
                            Commitment.status.not_in(
                                [CommitmentStatus.DONE, CommitmentStatus.CANCELLED]
                            )
                        )
                        .order_by(
                            Commitment.due_at.is_(None),
                            Commitment.due_at,
                            Commitment.created_at,
                        )
                        .limit(20)
                    )
                ).all()
            )
            tasks = list(
                (
                    await session.scalars(
                        select(Task)
                        .where(Task.status == TaskStatus.PENDING)
                        .order_by(Task.due_at.is_(None), Task.due_at, Task.created_at)
                        .limit(20)
                    )
                ).all()
            )
            closed_commitments = list(
                (
                    await session.scalars(
                        select(Commitment)
                        .where(
                            Commitment.status.in_(
                                [CommitmentStatus.DONE, CommitmentStatus.CANCELLED]
                            )
                        )
                        .order_by(Commitment.updated_at.desc())
                        .limit(20)
                    )
                ).all()
            )
            closed_tasks = list(
                (
                    await session.scalars(
                        select(Task)
                        .where(Task.status.in_([TaskStatus.DONE, TaskStatus.CANCELLED]))
                        .order_by(Task.updated_at.desc())
                        .limit(20)
                    )
                ).all()
            )
            recent_replies = list(
                (
                    await session.scalars(
                        select(Event)
                        .where(
                            Event.source == EventSource.TELEGRAM,
                            Event.direction == EventDirection.OUTBOUND,
                            Event.event_type == "assistant.reply",
                            Event.conversation_external_id == str(chat.id),
                            Event.content_text.is_not(None),
                        )
                        .order_by(Event.occurred_at.desc())
                        .limit(10)
                    )
                ).all()
            )

            active_items = [
                (commitment.id, commitment.summary, "active")
                for commitment in commitments
                if resolution.item_kind != "task"
            ] + [
                (task.id, task.title, "active")
                for task in tasks
                if resolution.item_kind != "commitment"
            ]
            reference_items = [
                *active_items,
                *[
                    (commitment.id, commitment.summary, commitment.status.value)
                    for commitment in closed_commitments
                    if resolution.item_kind != "task"
                ],
                *[
                    (task.id, task.title, task.status.value)
                    for task in closed_tasks
                    if resolution.item_kind != "commitment"
                ],
            ]
            displayed = await self._latest_displayed_items(
                session, recent_replies, reference_items, resolution.item_kind
            )

        if resolution.all_visible and displayed:
            cancelled = 0
            for item_id, _title, state in displayed:
                if state == "active" and await self._reminder_service.cancel_commitment(item_id):
                    cancelled += 1
            await message.reply_text(
                f"✅ בוטלו {cancelled} פריטים מהרשימה האחרונה שהצגתי."
                if cancelled
                else "ℹ️ כל הפריטים ברשימה האחרונה כבר טופלו."
            )
            return
        target: tuple[uuid.UUID, str, str] | None = None
        if resolution.ordinal is not None:
            if 1 <= resolution.ordinal <= len(displayed):
                target = displayed[resolution.ordinal - 1]
        elif resolution.title_hint:
            normalized_hint = self._normalize_item_reference(resolution.title_hint)
            ranked = sorted(
                (
                    max(
                        SequenceMatcher(
                            None, normalized_hint, self._normalize_item_reference(title)
                        ).ratio(),
                        1.0 if normalized_hint in self._normalize_item_reference(title) else 0.0,
                    ),
                    item_id,
                    title,
                    state,
                )
                for item_id, title, state in reference_items
            )
            if ranked and ranked[-1][0] >= 0.60:
                score, item_id, title, state = ranked[-1]
                second_score = ranked[-2][0] if len(ranked) > 1 else 0.0
                if len(ranked) == 1 or score - second_score >= 0.08:
                    target = (item_id, title, state)
        elif not resolution.all_visible and len(displayed) == 1:
            target = displayed[0]

        if target is None:
            verb = "סמן כבוצע" if resolution.action == "done" else "בטל"
            callback_action = "done" if resolution.action == "done" else "drop"
            options = [
                [
                    InlineKeyboardButton(
                        f"{verb}: {title[:32]}",
                        callback_data=f"{callback_action}:{item_id}",
                    )
                ]
                for item_id, title, _state in active_items[:4]
            ]
            await message.reply_text(
                (
                    "אין לי רשימה אחרונה מזוהה לביטול. הצג את המשימות או ההתחייבויות, "
                    "ואז כתוב „תמחק הכל” כדי לבטל רק את הפריטים שיוצגו."
                    if resolution.all_visible
                    else "לא ברור לי לאיזה פריט התכוונת. בחר פריט או כתוב את שמו:"
                ),
                reply_markup=InlineKeyboardMarkup(options) if options else None,
            )
            return

        item_id, title, state = target
        if state != "active":
            state_label = "כבר סומן כבוצע" if state == "done" else "כבר בוטל"
            await message.reply_text(f"ℹ️ הפריט „{title}” {state_label}. לא שיניתי פריטים אחרים.")
            return
        handled = (
            await self._reminder_service.mark_done(item_id)
            if resolution.action == "done"
            else await self._reminder_service.cancel_commitment(item_id)
        )
        if handled:
            result = "סומן כבוצע" if resolution.action == "done" else "בוטל"
            await message.reply_text(f"✅ הפריט „{title}” {result}. שאר הרשימה לא השתנתה.")
        else:
            await message.reply_text("הפריט כבר טופל או שאינו פעיל.")

    async def _latest_displayed_items(
        self,
        session: AsyncSession,
        replies: list[Event],
        reference_items: list[tuple[uuid.UUID, str, str]],
        item_kind: Literal["task", "commitment"] | None,
    ) -> list[tuple[uuid.UUID, str, str]]:
        """Resolve only the newest shown list, never guess from older lists or prose."""
        for reply in replies:
            payload = reply.payload_json or {}
            if "visible_item_ids" in payload:
                kind = payload.get("visible_item_kind")
                if kind not in {"task", "commitment"} or item_kind not in {None, kind}:
                    return []
                displayed = []
                for raw_id in payload["visible_item_ids"]:
                    try:
                        item_id = uuid.UUID(raw_id)
                    except (TypeError, ValueError, AttributeError):
                        return []
                    if kind == "task":
                        task = await session.get(Task, item_id)
                        if task is None:
                            return []
                        title = task.title
                        status = task.status.value
                    else:
                        commitment = await session.get(Commitment, item_id)
                        if commitment is None:
                            return []
                        title = commitment.summary
                        status = commitment.status.value
                    state = status if status in {"done", "cancelled"} else "active"
                    displayed.append((item_id, title, state))
                return displayed
            lines = re.findall(
                r"^\s*(?:[-•*]|\d+[.)])\s+(.+)$", reply.content_text or "", re.MULTILINE
            )
            if not lines:
                continue
            displayed = []
            for line in lines:
                normalized = self._normalize_item_reference(line)
                matches = [
                    item
                    for item in reference_items
                    if normalized == self._normalize_item_reference(item[1])
                    or normalized.startswith(self._normalize_item_reference(item[1]) + " ")
                ]
                if len(matches) != 1 or matches[0] in displayed:
                    return []
                displayed.append(matches[0])
            return displayed
        return []

    @staticmethod
    def _normalize_item_reference(value: str) -> str:
        return " ".join(re.sub(r"[^\w\s]", " ", value.casefold()).split())

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
        except LLMRetryableError as exc:
            logger.warning(
                "telegram_gemini_temporarily_unavailable",
                extra={
                    "error_type": type(exc).__name__,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
            )
            await message.reply_text(retryable_llm_message(exc))
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
        try:
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
                if self._conversation_service is None:
                    await message.reply_text(NO_ACTION_MESSAGE)
                    return
                await self._conversation_service.respond(
                    uuid.UUID(result.event_id),
                    str(chat.id),
                    source_text,
                )
        except LLMRetryableError as exc:
            logger.warning(
                "telegram_gemini_temporarily_unavailable",
                extra={
                    "error_type": type(exc).__name__,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
            )
            await message.reply_text(retryable_llm_message(exc))

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
        if await self._handle_manual_time_reply(message, str(chat.id)):
            return
        if await self._handle_spoken_request(update):
            return
        if self._control.paused:
            await message.reply_text("הסוכן מושהה. השתמש ב־/resume.")
            return
        if self._intake_service is None:
            await message.reply_text("שירות הקליטה עדיין לא מוכן.")
            return
        try:
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
                if self._conversation_service is None:
                    await message.reply_text(NO_ACTION_MESSAGE)
                    return
                await self._conversation_service.respond(
                    uuid.UUID(result.event_id),
                    str(chat.id),
                    message.text,
                )
        except LLMRetryableError as exc:
            logger.warning(
                "telegram_gemini_temporarily_unavailable",
                extra={
                    "error_type": type(exc).__name__,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
            )
            await message.reply_text(retryable_llm_message(exc))
