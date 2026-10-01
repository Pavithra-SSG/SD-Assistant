# Go-live checklist

For the person responsible for switching the service desk on. Work top to bottom; each item says who does
it and how you know it's done.

## 1. Rotate the TypeSafe key (today — 5 minutes, you)

The key used during development was shared in a chat, so treat it as exposed. The app can't do this for you:
only the account owner can, in the TypeSafe console.

1. Sign in at console.typesafe.ai → API keys → **Create new key**. Copy it.
2. Put it in `.env` (development) and `.env.production` (server) as `TYPESAFE_API_KEY=…`.
3. Restart: development `uvicorn api:app`; server `dc up -d api`.
4. Check: `dc exec api python manage.py check` shows the model name, not "MOCK".
5. Back in the console, **revoke the old key**. Revoking is the step that actually protects you.

Do the same for any other secret that was ever pasted into a chat, e-mail or ticket (`JWT_SECRET` signs
everyone out when changed; database passwords need `dc up -d` after the change).

## 2. Install and connect (IT operations)

Follow [DEPLOY.md](DEPLOY.md). Done when `dc ps` shows every service up and `manage.py check` ends with `OK`.
Before go-live, fill the "strongly recommended" block of `.env.production`:

| Setting | Why it matters | Test |
|---|---|---|
| `SMTP_*` | Employees hear back when an engineer replies, so slow replies don't look like silence | Comment on a test ticket; the employee gets an e-mail within a minute |
| `TEAMS_WEBHOOK_URL` | P1/P2 alerts reach the team even when nobody has the app open | Raise a test P1 ("my laptop battery is swelling"); a message appears in the channel |
| `ALERT_WEBHOOK_URL` / `ALERT_EMAIL_TO` | The watchdog tells IT when the service desk itself is broken | `dc stop worker`; within ~3 minutes a "DOWN" message arrives; `dc start worker` → "RECOVERED" |
| `HEALTHCHECK_PING_URL` | An outside service alerts you if the whole server dies (the watchdog can't report its own death) | Create a check at healthchecks.io (period 1 minute, grace 5), paste its ping URL, confirm it shows "up" |

## 3. Content (service desk lead, 1–2 days)

- [ ] **Approve the employee-friendly articles.** Knowledge → each article → compare the draft with the
      original, fix button names and paths to match your company's tools (Company Portal, VPN app, meeting
      app), run the meaning check, **Approve and publish**. Until an article is approved the bot uses the plain
      article text, which is written for agents.
- [ ] Review the saved replies (Knowledge → Saved replies) and add your own.
- [ ] Put your public holidays in a file and set `HOLIDAYS_FILE`, so SLA targets and ETAs skip them.

## 4. People (HR + service desk lead)

- [ ] Create the supervisor account(s) (`manage.py create-user … supervisor`).
- [ ] Import the pilot team and the IT staff from HR (User accounts → Import from HR).
- [ ] Hand out temporary passwords through a verified channel.

## 5. Privacy (data protection officer / legal)

- [ ] Review and adopt [PRIVACY.md](PRIVACY.md) (a draft, not legal advice). Confirm the retention periods
      (`RETENTION_MONTHS`, `ATTACHMENT_RETENTION_DAYS`) and who may see tickets.
- [ ] Make the privacy notice available to employees (the sign-in page shows a short version and links to
      "download my data").

## 6. Pilot (2–3 weeks, one team)

See [PILOT.md](PILOT.md). Go company-wide only when its exit criteria are met.
