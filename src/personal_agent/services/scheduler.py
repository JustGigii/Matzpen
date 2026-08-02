import asyncio
import logging

from personal_agent.services.briefs import MorningBriefService
from personal_agent.services.control import AgentControl
from personal_agent.services.lifecycle import LifecycleService
from personal_agent.services.reminders import ReminderService
from personal_agent.services.whatsapp import WhatsAppService

logger = logging.getLogger(__name__)


class SchedulerRuntime:
    """Small persistent-state polling loop for due internal reminders."""

    def __init__(
        self,
        reminders: ReminderService,
        briefs: MorningBriefService,
        lifecycle: LifecycleService,
        control: AgentControl,
        interval_seconds: int,
        whatsapp: WhatsAppService | None = None,
    ) -> None:
        self._reminders = reminders
        self._briefs = briefs
        self._lifecycle = lifecycle
        self._control = control
        self._interval_seconds = interval_seconds
        self._whatsapp = whatsapp
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="personal-agent-scheduler")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        first_cycle = True
        while not self._stop_event.is_set():
            if not self._control.paused:
                operations = [
                    self._reminders.execute_due_internal_actions,
                    self._reminders.resolve_due_clarifications,
                    self._reminders.dispatch_due_reminders,
                    self._reminders.mark_overdue,
                    self._reminders.expire_approvals,
                    self._lifecycle.recover_configured_calendar_actions,
                ]
                if self._whatsapp is not None:
                    operations.extend(
                        [
                            self._whatsapp.flush_due_buffers,
                            self._whatsapp.refresh_archives_if_due,
                            self._whatsapp.run_initial_history_review,
                            self._whatsapp.poll_connectivity,
                            self._whatsapp.send_disconnect_warning_if_due,
                            self._whatsapp.redact_expired_events,
                        ]
                    )
                if not first_cycle:
                    operations.append(self._briefs.trigger_fallback_if_due)
                for operation in operations:
                    try:
                        await operation()
                    except Exception:
                        logger.exception("Scheduled operation failed: %s", operation.__name__)
            first_cycle = False
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                continue
