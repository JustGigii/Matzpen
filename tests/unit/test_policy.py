from personal_agent.domain.enums import ActionClass
from personal_agent.services.policy import ApprovalPolicy


def test_internal_action_has_cancellable_grace_period() -> None:
    decision = ApprovalPolicy(60).decide(ActionClass.INTERNAL_REVERSIBLE)

    assert decision.execute_automatically is True
    assert decision.notify_before_execution is True
    assert decision.explicit_approval_required is False
    assert decision.grace_seconds == 60


def test_external_communication_cannot_execute_without_approval() -> None:
    decision = ApprovalPolicy(60).decide(ActionClass.EXTERNAL_COMMUNICATION)

    assert decision.execute_automatically is False
    assert decision.explicit_approval_required is True
