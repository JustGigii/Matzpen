# WhatsApp read-only deployment (aaPanel + OpenWA)

This integration observes one WhatsApp account and creates proposals/reminders in the existing
personal-agent workflow. It never sends, replies, reacts, edits, deletes, or marks a WhatsApp
message read. OpenWA is an unofficial WhatsApp Web client, so account restriction risk is non-zero.
Pairing must be an explicit manual decision; automated tests use `FakeOpenWAReadClient` only.

## Topology and version pinning

Run one personal-agent process and one OpenWA stack on the same VM:

```text
Internet -> aaPanel Nginx/TLS -> 127.0.0.1:8000 (personal-agent webhook)
                                127.0.0.1:2785 (OpenWA API + bundled dashboard, local only)
```

Clone the official OpenWA repository into a separate directory, inspect its release notes and
`openapi.json`, then pin an audited tag or commit. Do not deploy a floating `latest` image. The
official production Compose already binds the API to `127.0.0.1:${API_PORT:-2785}`.

```bash
cd /www/server
git clone https://github.com/rmyndharis/OpenWA.git openwa
cd openwa
git checkout <reviewed-tag-or-full-commit>
cp /path/to/Matzpen/deploy/openwa.env.example .env
# Replace every placeholder, then keep .env readable only by the service administrator.
docker compose config
docker compose build --pull
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:2785/api/health
```

The template selects `whatsapp-web.js`, SQLite, local session/media storage, no Redis/queue, one
session, automatic restore after a restart, disabled MCP, and disabled production Swagger. OpenWA's
dashboard is bundled into the API; localhost binding and the absence of an aaPanel proxy route are
what keep it private.

Before disabling Swagger, inspect `http://127.0.0.1:2785/api/docs` through an SSH tunnel and compare
the installed schemas with `src/personal_agent/integrations/openwa/schemas.py`. In particular, verify
GET session/message fields and whether chat summaries expose `isArchived`. The application refuses
to label archive filtering reliable when that field is unavailable, and suspends the initial
history review instead of scanning archived chats unsafely. In that case upgrade/pin a compatible
OpenWA version or populate `WHATSAPP_IGNORED_CHAT_IDS` until the capability is available.

## Personal-agent configuration

Generate separate random secrets for the OpenWA API and webhook HMAC. Never reuse the Telegram,
Gemini, aaPanel, or OpenWA administrator secret.

```env
OPENWA_BASE_URL=http://127.0.0.1:2785/api
OPENWA_API_KEY=<session-scoped-read-key-if-supported-by-the-pinned-version>
OPENWA_SESSION_ID=<session UUID returned by OpenWA, not its display name>
OPENWA_WEBHOOK_SECRET=<long-random-hmac-secret>
OPENWA_SIGNATURE_HEADER=X-OpenWA-Signature
OPENWA_WEBHOOK_MAX_BYTES=1048576

WHATSAPP_IGNORE_ARCHIVED=true
WHATSAPP_IGNORED_CHAT_IDS=
WHATSAPP_ARCHIVE_REFRESH_MINUTES=30
WHATSAPP_INITIAL_HISTORY_DAYS=7
WHATSAPP_HISTORY_MODE=review_only
WHATSAPP_HISTORY_MAX_MESSAGES=3000
WHATSAPP_HISTORY_MAX_MESSAGES_PER_CHAT=300
WHATSAPP_CONVERSATION_IDLE_SECONDS=300
WHATSAPP_URGENT_BYPASS_MINUTES=15
WHATSAPP_MAX_MEDIA_BYTES=18874368
WHATSAPP_EVENT_RETENTION_DAYS=30
WHATSAPP_DISCONNECT_WARNING_MINUTES=10
```

When personal-agent itself runs in Docker, use
`OPENWA_BASE_URL=http://host.docker.internal:2785/api`; the root Compose supplies the Linux
`host-gateway` mapping. OpenWA remains bound to host loopback.

Back up the SQLite database and then run:

```bash
alembic upgrade head
uvicorn personal_agent.main:app --host 127.0.0.1 --port 8000
curl --fail http://127.0.0.1:8000/health/ready
```

Install `deploy/aapanel-nginx.conf` inside a dedicated aaPanel TLS vhost. Replace/augment the denied
`/api/status` location with an IP allow-list or Basic Auth if remote status access is required.
There must be no Nginx route to port 2785, `/api/docs`, the OpenWA dashboard, or `/mcp`.

## Manual session creation and QR pairing

These are deliberate administrator actions and are never run by tests or application code.

```bash
export OPENWA_ADMIN_KEY='<OpenWA API_MASTER_KEY>'

curl -sS -X POST http://127.0.0.1:2785/api/sessions \
  -H "X-API-Key: ${OPENWA_ADMIN_KEY}" \
  -H 'Content-Type: application/json' \
  -d '{"name":"personal-primary","config":{"autoReconnect":true}}'

# Copy the returned session `id` into OPENWA_SESSION_ID.
curl -sS -X POST "http://127.0.0.1:2785/api/sessions/${OPENWA_SESSION_ID}/start" \
  -H "X-API-Key: ${OPENWA_ADMIN_KEY}"
curl -sS "http://127.0.0.1:2785/api/sessions/${OPENWA_SESSION_ID}/qr" \
  -H "X-API-Key: ${OPENWA_ADMIN_KEY}"
```

On the phone open WhatsApp **Settings -> Linked devices -> Link a device**, scan the fresh QR, and
wait until this returns `ready`:

```bash
curl -sS "http://127.0.0.1:2785/api/sessions/${OPENWA_SESSION_ID}" \
  -H "X-API-Key: ${OPENWA_ADMIN_KEY}"
```

Create a webhook from the local API using the installed Swagger. Register only the required receive
and lifecycle events (`message.received`, `message.sent`, `message.edited`, `message.revoked`,
`message.failed`, `call.received`, and `session.status`) and set:

```text
URL: https://<personal-agent-domain>/api/webhooks/openwa
secret: the exact OPENWA_WEBHOOK_SECRET value
```

Do not register a webhook test that sends WhatsApp content. Confirm a signed synthetic delivery or
an ordinary controlled chat instead. The application expects `X-OpenWA-Signature: sha256=<hex>` and
returns HTTP 202 after durable local intake/buffering.

## Backups, monitoring, and acceptance

- Encrypt backups of OpenWA `data/` because it contains linked-device session credentials.
- Back up personal-agent SQLite before every Alembic upgrade.
- Monitor `/health/ready`, authenticated `/api/status`, container health, disk space, and OpenWA
  session state. Do not log API keys, HMAC values, QR data, message bodies, or raw media.
- Raw WhatsApp event content is redacted after 30 days; IDs, timestamps, dedupe keys, derived items,
  people provenance, and audit records remain.
- Execute the 14 controlled real-world checks from `MILESTONE_3.md` only after QR pairing. In
  particular, restart during a buffer, test edit/revocation/archive/mention handling, disconnect and
  relink, and inspect the code/API surface again to prove no WhatsApp send operation exists.
