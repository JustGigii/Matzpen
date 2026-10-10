# iPhone morning brief Shortcut

Create a Shortcut that sends JSON to `POST /api/briefs/morning/trigger` with
`Authorization: Bearer <SHORTCUT_BEARER_TOKEN>` and body:

```json
{"source": "waking_up", "force": false}
```

Add one or more Personal Automation triggers:

- Sleep → Waking Up;
- wake-up alarm stopped;
- charger disconnected;
- opening Telegram or another chosen morning app; and
- a time-of-day fallback.

iOS does not expose a general phone-unlocked or phone-moved trigger. It is safe for several
automations to call the endpoint because the server returns the existing brief after the first
automatic trigger that local day. At `10:00` in `Asia/Jerusalem`, the backend also sends one daily
check-in with questions about completed items, priorities, missing dates, and items that need
rescheduling. This check-in still runs when a wake-up summary was sent earlier; successful delivery
is persisted so scheduler retries and process restarts do not repeat it. If the service is offline
at 10:00, it sends the check-in after it resumes that day. Set `force: true` only for an intentional
repeat delivery. Keep the application running and Telegram configured to receive these messages.

Expose the service only through private networking or a secured HTTPS reverse proxy; never put the
token in source control.
