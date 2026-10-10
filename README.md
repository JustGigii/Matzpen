# Personal Agent

An approval-gated personal agent for one user. Telegram and authenticated iPhone Shortcuts feed the
same normalized intake pipeline; Gemini returns typed extraction data; SQLite stores the complete
commitment, reminder, calendar-action, approval, audit, and morning-brief lifecycle.

The application never sends WhatsApp messages, email, attendee invitations, or messages to another
Telegram user. Tests use only fake Gemini, Telegram, Google Calendar, and OpenWA providers.

## Local setup

Requirements: Python 3.12 and, optionally, Docker.

```powershell
cd D:\project\Matzpen
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\uvicorn.exe personal_agent.main:app --host 127.0.0.1 --port 8000
```

Health and status routes:

- `GET /health/live`
- `GET /health/ready`
- `GET /api/status`
- `/docs`

Only one application instance may poll a Telegram bot token at a time.

## Commitment lifecycle

For a supported, deterministically valid extraction with confidence at least `0.90`, the bot sends a
preview and stores a pending action. The action executes after
`INTERNAL_ACTION_GRACE_SECONDS` (60 by default), unless the user confirms immediately, changes it,
or cancels it. Execution schedules durable reminders at the validated lead times and at the due
time for ordinary commitments.

Lower-confidence or ambiguous items remain approval/clarification requests. Approval executes the
entire downstream workflow and finishes as `executed`; repeated callbacks do nothing. An unanswered
time clarification uses a validated Gemini fallback after
`CLARIFICATION_FALLBACK_MINUTES` (10 by default). Pending requests expire at the due time or after
`APPROVAL_EXPIRY_HOURS` (24 by default), whichever comes first.

Untimed commitments have no exact-time reminders and remain in every daily brief until done or
cancelled. Timed commitments become overdue once after `OVERDUE_GRACE_MINUTES`; they are not flooded
with repeated immediate alerts.

Reminder buttons clearly separate **Finished** (the item was completed) from **Not relevant** (drop
it without claiming completion), plus remind-later, choose-time, 10-minute, and 1-hour actions. A
failed Telegram delivery returns to the durable queue and is retried on the next scheduler pass.
If a due-time alert gets no action, one follow-up is queued after the overdue grace period (15
minutes by default). The time picker includes a `Write time` Force Reply that accepts values such as
`19:30`, `tomorrow 08:15`
(in Hebrew), or `04.08 17:45`. Exact ISO times can also be supplied with:

```text
/reschedule <commitment-id> <ISO-time>
/resolve_time <approval-id> <ISO-time>
```

All scheduler decisions come from persisted rows, so grace periods, clarification fallbacks,
reminders, expiry, overdue transitions, and morning fallback processing recover after restart.

## Telegram

Create a bot with `@BotFather`, then configure only your own numeric user ID:

```env
TELEGRAM_BOT_TOKEN=<bot token>
TELEGRAM_ALLOWED_USER_IDS=<your numeric Telegram user ID>
TELEGRAM_MAX_MEDIA_BYTES=18000000
```

Multiple allowed IDs may be comma-separated. Every command, normal text message, and callback is
ownership-checked. A normal Telegram message becomes a normalized event deduplicated by chat and
message ID and follows the same extraction/lifecycle pipeline as other sources.

Telegram also accepts voice notes, audio files, PDF, DOCX, TXT/Markdown/CSV, and photos. Raw media
is downloaded into memory only, limited by `TELEGRAM_MAX_MEDIA_BYTES` (18MB by default), and is not
stored in the database. TXT and DOCX text is extracted locally; audio, PDF, and images are converted
to text through the configured Gemini provider. The bot sends the untrusted extracted text directly
through the normal approval-gated intake pipeline. Both text and media inputs show only the resulting
action card, without intermediate extraction, transcript, or technical count messages. A voice
question such as `מה יש היום?` is routed to the spoken-query handler instead of creating a
commitment.

When the intake pipeline finds no action to create, the message continues to Gemini as a normal
Telegram conversation. Gemini receives a bounded application context: up to 12 recent Telegram
turns, confirmed memory facts, active commitments/tasks, and the next seven days of Google Calendar
events. It does not receive database access, credentials, arbitrary API access, or a tool that can
perform actions. Any stable personal fact Gemini suggests is stored only as a proposal and appears
with **שמור** and **אל תשמור** buttons; only an explicit approval makes it confirmed memory.
`/memory` displays confirmed memories only. See
[`docs/telegram-gemini-memory.md`](docs/telegram-gemini-memory.md) for the exact context boundaries.

Commands:

```text
/start /status /today /calendar /tasks /commitments /memory
/pause /resume /reschedule /resolve_time /groups /help
```

`/today` returns a fresh daily summary without causing an automatic-delivery duplicate.
`/tasks` and the Hebrew request `מה המשימות שלי` render only open tasks as actionable cards.
The Hebrew requests `הצג הכל` and `מה אני צריך לעשות` show all open tasks and commitments,
including past and future dates and items without a date. Long lists are split into messages with
continuous numbering. `סגור הכל סיימתי` offers a confirmation button for the current set of open
items; items created after that request are excluded. After displaying a list, `תוסיף הכל ליומן`
adds the displayed timed items to the connected Google Calendar, without duplicate events.
Use `תוסיף את 2 ליומן` to select one item; untimed items require a date and time first.
Each card can mark the task done, choose a new due time, or cancel it; resolved tasks disappear
from the next task view and their remaining reminders are closed.

The development CLI remains available:

```powershell
.\.venv\python.exe scripts\process_message.py "אני צריך להתקשר לדניאל מחר ב-14:15"
```

It uses the same durable intake path as the bot and sends the same styled Telegram action cards.
Keep the application running to receive callback buttons and execute scheduled work. Reuse
`--event-id <stable-id>` to verify that the same source message is deduplicated.

## Gemini

```env
GEMINI_API_KEY=<key>
GEMINI_MODEL=<model available to the key>
```

Gemini is behind one provider interface. Extraction and time proposals are schema-constrained and
treated as untrusted data. Application code independently validates confidence, future times,
explicit dates, quiet hours, reminder bounds, calendar intervals, and the prohibition on attendees.
Only short decision summaries are stored; hidden reasoning is never requested or persisted.

The serving schema intentionally omits Pydantic constraints that can exceed Gemini's grammar-state
budget, and the provider retries only the documented schema-state error with JSON mode. Full
Pydantic validation remains mandatory. `explicit_date` is a date-only `YYYY-MM-DD`; actionable
datetimes must include a UTC offset.

Media extraction uses Gemini inline data only after Telegram and application size checks. Automated
tests use queued fake transcripts and never upload test files or require external credentials.

### LLM fallback providers

The runtime uses the first configured provider that succeeds, in this order: Gemini, Groq,
Cerebras, then Mistral OCR for supported documents and images. Quota and temporary service errors
move to the next provider and place the failed provider on a short in-memory cooldown. Invalid
credentials and invalid requests remain visible configuration errors instead of being hidden.

```env
GROQ_API_KEY=<key>
CEREBRAS_API_KEY=<key>
MISTRAL_API_KEY=<key>
```

Groq supplies structured text, image/OCR, and audio transcription fallbacks. Cerebras supplies a
second structured-text fallback. Mistral OCR handles PDF and image extraction. All are optional;
model defaults are listed in `.env.example`.

## Oracle Autonomous Database

Oracle uses the async `python-oracledb` Thin driver, so no Oracle Instant Client is required. Keep
credentials out of `DATABASE_URL`:

```env
DATABASE_URL=oracle+oracledb_async://@
ORACLE_USER=ADMIN
ORACLE_PASSWORD=<database-password>
ORACLE_DSN=<full TLS DSN from Oracle Cloud>
```

After setting those values, verify the connection, create the schema, and copy the current SQLite
data:

```powershell
.\.venv\python.exe scripts\check_oracle.py
.\.venv\python.exe -m alembic upgrade head
.\.venv\python.exe scripts\migrate_sqlite_to_oracle.py
.\.venv\python.exe scripts\verify_database_copy.py
```

The copy command only accepts an empty Oracle target, runs inserts in a transaction, and verifies
row counts. The final verification compares a canonical fingerprint of every field in every row.
Keep a backup of `data/personal_agent.db` until the application has been verified against Oracle.

## Google Calendar

Google Calendar is canonical for meetings, appointments, classes/course rows, interviews, and
submission deadlines. Every Calendar write requires explicit approval; it never uses the automatic
grace-period path. Timetable rows become idempotent recurring actions and rows marked excluded are
skipped. Assignment deadlines create deadline events and earlier reminders, not invented work
blocks.

The current local environment is connected and has been verified with successful Google Calendar
writes. The OAuth steps below are still required when setting up a new environment.

Any timed task or commitment can also be added with the **Add to Calendar** button in its Telegram
card. That explicit tap creates one idempotent 30-minute Calendar block; repeated taps do not create
duplicates.

OAuth setup:

1. Enable Google Calendar API in Google Cloud.
2. Configure the OAuth consent screen for your account.
3. Create a Desktop app OAuth client.
4. Save its JSON as `secrets/google-client.json`.
5. Run:

```powershell
New-Item -ItemType Directory -Force secrets
.\.venv\python.exe scripts\bootstrap_google_oauth.py `
  --client-secret-file secrets\google-client.json `
  --token-file secrets\google-token.json
```

Then set:

```env
GOOGLE_CLIENT_SECRET_FILE=secrets/google-client.json
GOOGLE_TOKEN_FILE=secrets/google-token.json
GOOGLE_CALENDAR_ID=primary
```

If OAuth is absent, the internal commitment and reminders are still saved and the calendar action
is marked `pending_configuration`. No attendees are ever added and Google writes use
`sendUpdates=none`.

## WhatsApp (OpenWA, read-only)

Milestone 3 adds a read-only OpenWA ingress. Signed webhook events are normalized and deduplicated,
locally filtered, grouped into durable five-minute conversation buffers, then passed to the same
untrusted Gemini and approval-gated lifecycle. Urgent user-authored commitments due within 15
minutes bypass the buffer once. Incoming requests remain proposals and are never accepted
automatically. Groups require user authorship or a direct mention/request; archived and denylisted
chats are excluded.

Private conversation follow-ups remain in the same durable buffer once the conversation becomes
relevant. This lets a sequence such as agreeing to a Zoom call, specifying “at 7”, and clarifying
“in the evening” produce separate meeting and link-sending items at 19:00. Timed meetings include a
Calendar proposal, while both the meeting and reminders remain pending until the user approves the
cards in Telegram.

The first seven days are a bounded, review-only scan with a durable watermark. Edits/revocations
invalidate pending work; changes to an already executed interpretation are surfaced without silent
mutation. Voice/audio, images, PDF, DOCX, TXT/Markdown/CSV use the transient media pipeline up to
18 MiB; video and unknown/oversized files are rejected. Raw WhatsApp content is redacted after 30
days while provenance and derived records remain.
Incoming group voice notes from other participants are downloaded and transcribed only after the
group is explicitly tracked, or when the message is directed to the user. The user's own group
voice notes remain eligible without group tracking. Raw audio bytes are never stored.
Prompts for newly discovered groups are disabled by default to avoid Telegram noise. Set
`WHATSAPP_PROMPT_NEW_GROUPS=true` if every new group should ask for a tracking decision; `/groups`
remains available for choosing groups manually.

The production adapter is intentionally GET-only and exposes no send/reply/react/edit/delete
method. OpenWA and its bundled dashboard stay on VM loopback; only the signed personal-agent
webhook is proxied through aaPanel TLS. Automated tests use fake payloads and a fake read client and
never pair a real account. See
[`docs/whatsapp-openwa-deployment.md`](docs/whatsapp-openwa-deployment.md) for version pinning,
aaPanel, backup, manual session creation, and QR pairing.

## Morning brief and iPhone automation

Create a random token with `scripts/create_shortcut_token.py`, store it as
`SHORTCUT_BEARER_TOKEN`, and call:

```text
POST /api/briefs/morning/trigger
Authorization: Bearer <shortcut-token>
Content-Type: application/json

{"source":"waking_up","force":false}
```

The endpoint uses `Asia/Jerusalem`, persists the content and trigger metadata, and deduplicates all
automatic calls to one delivery per local date. A duplicate receives the already-generated content.
`force=true` explicitly regenerates and delivers it.

iOS Shortcuts has no generic unlock or phone-moved trigger. Use one or several Personal Automation
approximations: Sleep → Waking Up, wake-up alarm stopped, charger disconnected, opening Telegram (or
another morning app), plus a time-of-day backup. They may all call the endpoint safely. At
`MORNING_BRIEF_FALLBACK_TIME` (default `10:00` in `Asia/Jerusalem`), the scheduler sends a daily
check-in with completion, priority, and scheduling questions, including when an earlier wake-up
brief was already delivered. Successful check-in delivery is persisted independently of the
wake-up brief. Keep the application running with Telegram configured; after downtime, the scheduler
sends that day's check-in when it resumes.

The brief combines today's Calendar events, all open commitments and tasks (past, future, and
untimed), full local dates and times, pending approvals, tight transitions/conflicts, and a priority
suggestion.

The existing text-intake endpoint remains:

```text
POST /api/intake/shortcut
Authorization: Bearer <shortcut-token>

{"type":"text","content":"תזכיר לי לחזור לדניאל היום בשש","captured_at":"2026-08-01T13:00:00+03:00","metadata":{"source":"siri"}}
```

Keep the API on localhost/private networking or behind a secured HTTPS reverse proxy.

## Database migration

Apply migrations through `20260802_0006_whatsapp_group_tracking` with `alembic upgrade head`. They add
durable reminder, calendar action, morning brief, WhatsApp session/conversation/buffer/history, and
people tables plus lifecycle/idempotency, redaction, and reminder-card fields. See
[`docs/migrations.md`](docs/migrations.md) for details.

## Quality checks

```powershell
.\.venv\Scripts\ruff.exe format .
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe src
.\.venv\Scripts\pytest.exe
git diff --check
```
