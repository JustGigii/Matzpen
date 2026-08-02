from enum import StrEnum


class EventSource(StrEnum):
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"
    CALENDAR = "calendar"
    SHORTCUT = "shortcut"
    GMAIL = "gmail"
    DESKTOP = "desktop"


class EventDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


class Sensitivity(StrEnum):
    NORMAL = "normal"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"


class ProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    FAILED = "failed"


class TaskStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    CANCELLED = "cancelled"


class CommitmentDirection(StrEnum):
    USER_PROMISED = "user_promised"
    OTHER_PROMISED = "other_promised"


class ActionType(StrEnum):
    CALL = "call"
    MESSAGE = "message"
    SEND = "send"
    MEET = "meet"
    PAY = "pay"
    OTHER = "other"


class CommitmentStatus(StrEnum):
    DETECTED = "detected"
    SCHEDULED = "scheduled"
    DONE = "done"
    CANCELLED = "cancelled"
    OVERDUE = "overdue"


class MemoryStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ActionClass(StrEnum):
    OBSERVE = "observe"
    INTERNAL_REVERSIBLE = "internal_reversible"
    EXTERNAL_COMMUNICATION = "external_communication"
    DESTRUCTIVE_OR_SENSITIVE = "destructive_or_sensitive"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


class ReminderKind(StrEnum):
    BEFORE_DUE = "before_due"
    DUE = "due"
    SNOOZE = "snooze"


class ReminderStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    HANDLED = "handled"
    CANCELLED = "cancelled"


class CalendarActionStatus(StrEnum):
    PENDING = "pending"
    PENDING_CONFIGURATION = "pending_configuration"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WhatsAppSessionStatus(StrEnum):
    UNKNOWN = "unknown"
    AUTHENTICATING = "authenticating"
    QR_REQUIRED = "qr_required"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


class WhatsAppInitialReviewStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class WhatsAppConversationType(StrEnum):
    PRIVATE = "private"
    GROUP = "group"
    UNKNOWN = "unknown"


class WhatsAppBufferStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class HistoricalFindingStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    IGNORED = "ignored"
    SUPERSEDED = "superseded"
