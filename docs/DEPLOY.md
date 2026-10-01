# Deploying the IT Service Desk (production)

For the person who installs and runs the service desk on a company server. Everything runs in Docker;
you need no Python on the server.

## What runs

| Container | What it does | Reachable from |
|---|---|---|
| `proxy` (Caddy) | HTTPS, redirects HTTP → HTTPS | Everyone: ports 80 and 443 (the only open ports) |
| `ui` (Streamlit) | The web app employees and IT staff use | Only the proxy |
| `api` (FastAPI) | All logic; the only process that changes data | Only `ui` and admin commands |
| `worker` | Every 15 s: alert timers, ticket sweeps, sending queued e-mails and Teams messages. Daily: deletes expired screenshots and removes personal text past its retention date | Nothing (outbound only) |
| `watchdog` | Checks the API and web app every minute; tells IT (Teams/e-mail) when the service desk is down or its worker has stopped | Nothing (outbound only) |
| `migrate` | Runs once at each start, as the database owner: schema changes and the restricted database accounts. Shows `Exited (0)` afterwards; that's normal | Nothing |
| `db` (Postgres 16) | All data, in the `pgdata` volume, split into areas (below) | Only the app containers |
| `backup` | `pg_dump` every 24 h into `./backups`, keeps 14 days | Nothing |

Screenshots are files in the `attachments` volume (not in the database, not in the backups: they are
short-lived by design and deleted after `ATTACHMENT_RETENTION_DAYS`).

### Database areas and accounts

| Area (schema) | Holds | The app (`sd_app`) may |
|---|---|---|
| `identity` | accounts, sign-in sessions | read, write |
| `service` | tickets, conversations, checklists, screenshots' records, ratings | read, write |
| `ops` | alerts, notifications, outbox, worker heartbeat, migration log | read, write (migration log: read only) |
| `audit` | who-did-what log, corrections | **add and read only**: edits and deletes are refused by the database |
| `knowledge` | article versions, bot answers log, feedback, knowledge gaps, saved replies | read, write |
| `reporting` | read-only views for BI tools (no names or message text) | — (`sd_reporting` may read these only) |

The owner account (`POSTGRES_USER`) is used only by `migrate` and `backup`. To connect Power BI or Excel, set
`DB_REPORTING_PASSWORD`, re-run `dc up -d`, and expose Postgres to the BI host only (add a firewall-limited
`ports:` entry to `db`); connect as `sd_reporting` to the `reporting` views.

The AI model (TypeSafe Jev) is called over HTTPS from the `api` container only; the key never reaches a browser.

## Server requirements

- Linux with Docker Engine 24+ and the Compose plugin (`docker compose version`)
- 2 vCPU, 4 GB RAM, 20 GB disk to start (grows with tickets; Postgres data is small, backups are the bulk)
- A DNS name for the app, e.g. `servicedesk.company.com`, pointing at the server
- Outbound HTTPS to `api.typesafe.ai`
- For a public certificate: inbound 80/443 from the internet. On a private network use Caddy's internal CA (below)

## First install

```bash
git clone <your repository> servicedesk && cd servicedesk
cp .env.production.example .env.production
chmod 600 .env.production
nano .env.production          # fill in every value under "required"
docker compose --env-file .env.production up -d --build
docker compose --env-file .env.production ps      # all services "Up", api/ui "(healthy)"
```

Generate each secret with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Use letters and digits
only for `POSTGRES_PASSWORD`, `DB_APP_PASSWORD` and `DB_REPORTING_PASSWORD` (they are placed inside a URL).
Fill the "strongly recommended" block too (e-mail, Teams, watchdog alerts); see [GO-LIVE.md](GO-LIVE.md).

Every later `docker compose` command also needs `--env-file .env.production`. To save typing:
`alias dc='docker compose --env-file .env.production'`.

### Create the first supervisor

Production starts with **no accounts** (the demo accounts exist only in development).

```bash
dc exec api python manage.py create-user EMP0001 "Your Name" you@company.com supervisor --title "Service Desk Supervisor"
dc exec api python manage.py check
```

The first command prints a one-time temporary password. Sign in at `https://<SITE_ADDRESS>`; you will be asked
to choose your own password. `check` must end with `OK`.

### Load employees and IT staff

Export from HR as CSV with this header (queues separated by `;`, only for agents):

```
employee_id,name,email,role,queues,job_title,department,location,asset_tag,manager_id
EMP1001,Priya Sharma,priya.sharma@company.com,employee,,Analyst,Finance,Chennai,LT-70001,EMP0900
EMP2010,Ananya Iyer,ananya.iyer@company.com,agent,Network Remote Access;Network Operations,Network Engineer,IT,Chennai,,
```

Roles: `employee`, `agent`, `supervisor`. Valid queue names are listed on the **User accounts** page.

Either in the app (**User accounts → Import from HR (CSV)**) or on the server:

```bash
dc exec -T api python manage.py import-users - < employees.csv > temporary-passwords.csv
```

The summary (created / updated / errors) prints on screen; `temporary-passwords.csv` holds the new accounts'
one-time passwords.

Re-importing the same file updates people instead of duplicating them. Distribute temporary passwords through
a verified channel, then delete the file. Everyone must change theirs at first sign-in.

### HTTPS certificates

- **Private network** (`CADDY_TLS=internal`): Caddy creates its own certificate authority. Install its root
  certificate on company devices once (e.g. through Intune or group policy) so browsers trust it:
  `docker cp servicedesk-proxy-1:/data/caddy/pki/authorities/local/root.crt ./servicedesk-root.crt`
- **Public DNS** (`CADDY_TLS=you@company.com`): Caddy gets and renews a Let's Encrypt certificate itself.

## Everyday operations

| Task | How |
|---|---|
| Someone forgot their password | Verify who they are (e.g. call back the number on file). **User accounts → Manage → Reset password**, or `dc exec api python manage.py reset-password EMP1001`. They get a one-time password and must change it. |
| "Can't sign in?" requests | Arrive as tickets in the **Identity Security** queue; handle as above. (Self-service reset stays off until a real identity provider is connected.) |
| Account locked (5 wrong passwords) | Unlocks itself after 15 minutes, or **Unlock** / `manage.py unlock ID` |
| Leaver | **Disable** / `manage.py disable ID`: signed out everywhere, can't sign in, history kept |
| New agent / changed team | **User accounts → Manage → Role and queues** / `manage.py set-role ID agent --queues "Q1;Q2"`; they sign in again |
| Who has access | `dc exec api python manage.py list-users --role supervisor` |

## Updating to a new version

```bash
git pull
# set APP_VERSION in .env.production to the new version
dc up -d --build
dc exec api python manage.py check
```

Database changes are applied automatically at start-up; they only add tables and columns, never remove data.
Take a backup first (below) for anything bigger than a patch release.

## Backups and restore

- A backup runs at start-up and then every 24 hours into `./backups/servicedesk-<time>.dump`; 14 days are kept.
- **Copy `./backups` off the server** every day (to a file share or cloud storage). A backup on the same disk is
  lost with the disk.
- Backup on demand: `dc exec backup sh -c 'pg_dump --format=custom --file=/backups/manual-$(date +%F).dump'`

Restore (replaces all data; users are signed out):

```bash
dc stop api worker ui
docker cp backups/servicedesk-20260930T020000Z.dump servicedesk-db-1:/tmp/restore.dump
dc exec db sh -c 'dropdb -U $POSTGRES_USER $POSTGRES_DB && createdb -U $POSTGRES_USER $POSTGRES_DB && \
  pg_restore -U $POSTGRES_USER -d $POSTGRES_DB --no-owner /tmp/restore.dump && rm /tmp/restore.dump'
dc start api worker ui
```

Test a restore every few months into a scratch database (`createdb restore_check` … `dropdb restore_check`), so
you know the backups work before you need them.

## Monitoring

| Check | Healthy | Meaning when not |
|---|---|---|
| `https://<SITE_ADDRESS>` | sign-in page | proxy or UI down |
| `dc exec api python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8000/ready').read())"` | `"ready":true` | `database:false`: Postgres unreachable · `worker:false`: **alerts have stopped**; restart the worker |
| `dc ps` | api/ui `(healthy)`, `migrate` `Exited (0)` | a container keeps restarting, or `migrate` exited non-zero: read its logs |
| Watchdog | Teams/e-mail stay quiet | "🚨 DOWN" message: follow its hint; "✅ RECOVERED" follows automatically |
| Outbox | `dc exec db psql -U $POSTGRES_USER -d $POSTGRES_DB -c "select status,count(*) from ops.outbox group by 1"` | many `failed`: check the SMTP settings / webhook URL (`last_error` column says why) |
| `dc logs --since 1h api worker` | JSON lines, level INFO | `ERROR` lines include a `request_id`, matching the `X-Request-ID` header |
| AI model | `/health` shows `"degraded": null` | recent Jev failures: the bot routes everything to people and supervisors see a "Model degraded" banner |

Logs rotate automatically (5 × 10 MB per container). To keep them longer, ship Docker's JSON logs to your log
platform.

## Security notes

- Only ports 80/443 are exposed. The API has no public route; its interactive docs are off (`DOCS_ENABLED=0`).
- Passwords: bcrypt; at least 12 characters, common passwords refused; 5 failures lock the account for 15 minutes;
  20 sign-in attempts per minute per network address. Sessions: 8 hours at most, 30 minutes idle.
- Containers run as an unprivileged user with a read-only file system and no Linux capabilities.
- Every sign-in, role denial, account change and ticket action is recorded in the `events` table. Nothing is
  deleted.
- **Rotating `JWT_SECRET`** signs everyone out; do it if the file may have leaked. **Rotating the TypeSafe key**:
  change `TYPESAFE_API_KEY`, then `dc up -d api`.
- Still simulated (see README "Honest limitations"): the identity step-up for self-service reset (kept off) and
  the device/identity tools the bot calls. Replace `servicedesk/tools.py` with real integrations before
  enabling them.

## Scaling

One `api` process with a thread pool serves a typical service desk (most time is spent waiting for the AI
model). Keep it at one: the lock that stops two messages in the same chat from being answered at the same time
lives in that process. To scale out, move that lock into Postgres (advisory lock per session) first. The
`worker` can run as two copies for resilience; only one runs the timers at a time (a lease in the `heartbeats`
table), and the other takes over within 45 seconds.

## Moving pilot data from SQLite

```bash
python scripts/sqlite_to_postgres.py servicedesk.db postgresql://servicedesk:<password>@<host>:5432/servicedesk
```

Copies every table, skips rows that already exist (safe to re-run) and prints row counts from both sides.
Demo accounts copied this way keep the demo password: disable or reset them afterwards.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `MigrationRequired: … run python manage.py migrate` | the app started before `migrate` finished, or `migrate` failed: `dc logs migrate` |
| Screenshots: "isn't an image I can read" | only PNG, JPG and WebP up to 10 MB are accepted |
| Bot says it couldn't read a screenshot | photos of a screen are hard to read; the employee is asked to type the error, which is expected |
| `ConfigError: JWT_SECRET is required` | `.env.production` missing, or started without `--env-file .env.production` |
| `set POSTGRES_PASSWORD` when starting | same: compose was started without `--env-file .env.production` |
| Browser warns about the certificate | internal CA root not installed on that device, or `SITE_ADDRESS` differs from the name typed |
| Everyone is sent to "Set your password" after an import | expected: imported accounts start with temporary passwords |
| Alerts stopped re-alerting | worker down: `dc ps worker`, `dc logs worker`, `dc restart worker` |
| "Too many sign-in attempts from this network" | 20 attempts/min from one address; wait a minute or raise `LOGIN_RATE_PER_MINUTE` if many people share one NAT address |
