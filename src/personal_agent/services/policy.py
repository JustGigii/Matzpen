from dataclasses import dataclass

from personal_agent.domain.enums import ActionClass


@dataclass(frozen=True)
class PolicyDecision:
    execute_automatically: bool
    notify_before_execution: bool
    explicit_approval_required: bool
    grace_seconds: int | None = None


class ApprovalPolicy:
    def __init__(self, internal_grace_seconds: int) -> None:
        self._internal_grace_seconds = internal_grace_seconds

    def decide(self, action_class: ActionClass) -> PolicyDecision:
        if action_class is ActionClass.OBSERVE:
            return PolicyDecision(True, False, False)
        if action_class is ActionClass.INTERNAL_REVERSIBLE:
            return PolicyDecision(
                execute_automatically=True,
                notify_before_execution=True,
                explicit_approval_required=False,
                grace_seconds=self._internal_grace_seconds,
            )
        return PolicyDecision(
            execute_automatically=False,
            notify_before_execution=True,
            explicit_approval_required=True,
        )
