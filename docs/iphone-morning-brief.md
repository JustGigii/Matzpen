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
automatic trigger that local day. The backend's own default `10:00` fallback covers mornings when
no automation fired. Set `force: true` only for an intentional repeat delivery.

Expose the service only through private networking or a secured HTTPS reverse proxy; never put the
token in source control.
