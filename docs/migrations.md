# Migration notes

## `20260801_0002_commitment_lifecycle`

Run:

```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

The migration adds:

- `reminders`, with one stable dedupe key per reminder schedule;
- `calendar_actions`, including payload, status, deterministic Google ID, recurrence ID, and
  execution timestamp;
- `morning_briefs`, unique by local date;
- commitment resolution, overdue, daily-surfacing, source-approval, calendar-action, and stable
  dedupe fields;
- stable dedupe/source-event fields on approvals; and
- an optional task dedupe key.

Existing commitments receive a `legacy:<id>` dedupe key. The downgrade removes milestone-2 tables
and fields but necessarily discards their lifecycle history.

## `20260801_0003_schema_compatibility`

This compatibility revision upgrades databases that had already recorded revision `0002` before
the final task-reminder and approval-linked Calendar fields were added. It makes reminder
`commitment_id` nullable, adds `reminders.task_id`, and adds `calendar_actions.approval_id` with
their foreign keys. The checks are conditional, so it is also safe when those columns already
exist.

## `20260801_0004_reminder_message_ids`

Adds nullable `reminders.telegram_message_id`. New reminder deliveries persist their Telegram
message ID so completing, cancelling, snoozing, or rescheduling one reminder can remove the stale
buttons from every related reminder card. Existing historical reminders remain valid but cannot be
backfilled because Telegram does not expose their message IDs through the database.

## `20260801_0005_whatsapp_read_model`

Adds the durable read model for the read-only OpenWA integration:

- `events.redacted_at`, `events.revoked_at`, and self-referencing `events.supersedes_event_id`;
- `persons`, uniquely keyed by channel and stable external identity;
- `whatsapp_session_states`, including connectivity incidents, initial-history watermark/status,
  and archive-filter reliability;
- `whatsapp_conversations`, unique by OpenWA session and external chat;
- `whatsapp_conversation_buffers`, with ordered source event IDs and a unique batch dedupe key; and
- `whatsapp_historical_findings`, with review status, confidence, provenance, and a unique dedupe
  key.

Back up both `data/personal_agent.db` and OpenWA's encrypted session/data backup before upgrading.
The downgrade removes these tables/columns and therefore discards WhatsApp review and retention
state, but does not alter OpenWA's separate SQLite/session files.

## `20260802_0006_whatsapp_group_tracking`

Adds durable opt-in state to `whatsapp_conversations`:

- `tracking_enabled` records the user's explicit Telegram choice for each group; and
- `tracking_prompted_at` prevents repeated prompts for the same discovered group.

Disabling tracking also cancels any still-pending conversation buffer for that group. The
downgrade removes only these two fields and does not delete WhatsApp messages or OpenWA state.

## SQLite to Oracle Autonomous Database

The ORM and historical migrations use portable JSON storage: SQLite stores JSON text and Oracle
stores it in CLOB columns. Application timestamps are normalized to UTC before Oracle stores them.

1. Back up `data/personal_agent.db`.
2. Configure `DATABASE_URL=oracle+oracledb_async://@` plus `ORACLE_USER`, `ORACLE_PASSWORD`, and
   `ORACLE_DSN` in `.env`.
3. Run `.\.venv\python.exe scripts\check_oracle.py`.
4. Run `.\.venv\python.exe -m alembic upgrade head` against the empty Oracle schema.
5. Run `.\.venv\python.exe scripts\migrate_sqlite_to_oracle.py`.
6. Run `.\.venv\python.exe scripts\verify_database_copy.py`.

The transfer refuses to run if any application table in Oracle already contains data. It defers
the self-reference on superseded events until all event rows exist, then compares every source and
target table count before reporting success.

## `20260804_0007_oracle_timestamp_precision`

Oracle maps generic SQLAlchemy `DateTime` to `DATE`, which does not preserve fractional seconds.
This Oracle-only migration changes every application UTC timestamp column to `TIMESTAMP`; SQLite
is intentionally unchanged. The ORM normalizes values to UTC, stores a timezone-free Oracle
`TIMESTAMP`, and restores UTC awareness when reading. The database-copy verifier compares all
fields and detects any lost timestamp precision, JSON changes, or text encoding differences.
