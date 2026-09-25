# Job Alert Bot

A single-user Telegram bot that watches hh.ru and rabota.by for new developer
vacancies, scores each one against your resume with Claude, and messages you
the ones that clear your threshold, usually within a few minutes of posting.

## How it works

- Every `POLL_INTERVAL_SECONDS` (default 5 min) it searches each site with your
  structured filters (text, role, experience, region, extra params), newest first.
- Postings it has never seen are fetched in full and scored 0-100 by Claude
  against your resume. Scores at or above `SCORE_THRESHOLD` (default 60) are
  sent to you; everything is recorded in sqlite so nothing is scored or sent twice.
- On the very first run it scores only the newest `FIRST_RUN_BATCH` (default 10)
  postings per site and marks the rest as seen, so you don't get a flood.
- If scoring or sending fails, the posting is retried next cycle rather than
  dropped. If one site's API is down, it backs off that site and keeps polling
  the other.
- Once a day at `HEARTBEAT_HOUR` it sends a "still running" message with counts.
  If that message stops arriving, the bot is down.

hh.ru and rabota.by are served by the same HeadHunter API (`api.hh.ru`); the
`host` parameter selects the site. They share one vacancy database, so a
vacancy that appears on both is notified once.

## Telegram commands

- `/start` registers you. The first chat to send it becomes the only user; everyone else is ignored.
- `/resume <text>` sets the resume used for scoring (or send a `.txt` file).
- `/status` shows counts, source health and whether a resume is set.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in the three required values
python -m job_alert
```

Then open your bot in Telegram, send `/start`, then `/resume` with your resume.
You can also put your resume in `resume.txt` (gitignored) or `RESUME_TEXT`.

Tests: `pip install pytest && python -m pytest`

## Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | | From @BotFather |
| `ANTHROPIC_API_KEY` | yes | | From console.anthropic.com |
| `HH_USER_AGENT` | yes | | hh.ru rejects requests without one. Format: `AppName/1.0 (you@example.com)` |
| `TELEGRAM_CHAT_ID` | no | | Pin the owner chat instead of capturing it from `/start` |
| `HH_ACCESS_TOKEN` | no | | hh.ru application token from dev.hh.ru, only if anonymous search gets blocked |
| `SOURCES` | no | `hh.ru,rabota.by` | |
| `SEARCH_TEXT` | no | `python` | hh.ru search query syntax |
| `SEARCH_EXPERIENCE` | no | any | `noExperience`, `between1And3`, `between3And6`, `moreThan6` |
| `SEARCH_PROFESSIONAL_ROLES` | no | `96` | Comma-separated hh.ru role ids (96 = programmer) |
| `AREA_HH_RU` | no | none | Region id for hh.ru (113 Russia, 1 Moscow) |
| `AREA_RABOTA_BY` | no | `16` | Region id for rabota.by (16 Belarus, 1002 Minsk) |
| `SEARCH_EXTRA_PARAMS` | no | | Any other `/vacancies` params as a query string, e.g. `work_format=REMOTE&only_with_salary=true` |
| `SCORE_THRESHOLD` | no | `60` | Minimum score to notify |
| `POLL_INTERVAL_SECONDS` | no | `300` | Minimum 60 |
| `SEARCH_PER_PAGE` | no | `50` | Postings fetched per poll (max 100) |
| `FIRST_RUN_BATCH` | no | `10` | Postings scored per site on the first run |
| `CLAUDE_MODEL` | no | `claude-opus-5` | `claude-haiku-4-5` is much cheaper if cost matters more than judgment |
| `CLAUDE_EFFORT` | no | `low` | Claude effort level for scoring |
| `HEARTBEAT_HOUR` | no | `9` | Hour of the daily heartbeat, in `TIMEZONE` |
| `TIMEZONE` | no | `Europe/Minsk` | |
| `DATA_DIR` | no | `./data` | Where the sqlite database lives. Falls back to `RAILWAY_VOLUME_MOUNT_PATH` |
| `RESUME_FILE` / `RESUME_TEXT` | no | `resume.txt` / empty | Used only if no resume was set via `/resume` |

## Deploy to Railway

The repo includes `railway.json` (Railpack build, `python -m job_alert`, one
replica, always restart). To deploy:

1. Create a Railway project from this GitHub repo.
2. **Add a volume** to the service (mount path e.g. `/data`). Without it the
   sqlite database, including your registered chat and resume, is wiped on every
   deploy and the bot re-runs its first-run batch. The bot picks up the mount
   path automatically via `RAILWAY_VOLUME_MOUNT_PATH`; set `DATA_DIR` only if
   you want a different path.
3. Set the variables: `TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `HH_USER_AGENT`,
   plus any optional ones from the table above. Don't upload `.env`.
4. Deploy. The service needs no public domain: it long-polls Telegram and makes
   only outbound requests.
5. Send `/start` and then `/resume` to the bot.

Keep it at exactly one replica: two instances would fight over Telegram's
`getUpdates` and double-poll the job sites.

## Not in v1

LinkedIn, Telegram job channels, dev.by, career.habr.com, superjob.ru and the
remote boards are out of scope for now; see the spec for why.
