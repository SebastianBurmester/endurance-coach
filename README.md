# Endurance Coach for Claude

A local MCP server and dashboard that pull rides, runs, swims and health data from **Garmin** (official Personal
API) or **COROS** (official COROS MCP server), cache them, and compute training metrics from the original FIT
files, so Claude can coach you for **road cycling** or **triathlon**. Data source and focus are chosen in the
dashboard's **Settings** tab.

**What it computes:** NP / IF / TSS / VI / EF and the power curve with FTP / CP estimates, rTSS from grade-adjusted
pace (NGP), sTSS vs CSS, hrTSS from the HR stream vs per-sport LTHR, Pw:HR and Pa:HR decoupling, HR and power zones,
laps, a combined Performance Management Chart (CTL / ATL / TSB / ramp / ACWR) with weekly volume per sport, and
morning readiness from HRV (vs the watch's normal range and 7/60-day baselines), resting HR, sleep, stress and the
watch's recovery gauge (Garmin Body Battery or COROS recovery), and a sleep analysis (rhythm and regularity, sleep
debt, stages, and how training load, late sessions, evening yoga, stress and bedtime relate to the night after).
The dashboard adds Races (countdown, projected race-day form, taper, race plan), per-sport views (volume, run ramp
checks, zones, power curve), Progress (thresholds, CTL, efficiency and pace trends), Recovery (readiness calendar,
strain episodes, bounce-back after big days), Nutrition (weight trend vs target with manual weigh-ins, W/kg and
time per kg on the key climbs, carbs / protein per planned day, session and race fueling, training energy) and a
page per session (streams, laps, intervals, zones). Plans, plan-vs-actual and a load projection are stored locally
and shown on the dashboard.

## One checkout per athlete

Everything personal lives next to the code and is never committed: `settings.json` (source and focus),
`athlete.json` (thresholds, goals, events), `data/` (SQLite cache, FIT files, logins in `data/auth/`),
`FOCUS.local.md` (imported by CLAUDE.md, points at `coaching/road-cycling.md` or `coaching/triathlon.md`), `.mcp.json`
and `coach.env`. Two athletes on one machine = two checkouts of this repo.

## Setup

1. **Install:** `python -m venv .venv` then `.venv/bin/pip install -e .` (Windows: `.venv\Scripts\pip`).
2. **Claude Code files for this checkout:** `.venv/bin/coach setup --source garmin --focus road_cycling`
   (or `--source coros --focus triathlon`). Writes `settings.json`, `.mcp.json` and `.claude/settings.local.json`.
3. **Dashboard:** `.venv/bin/coach web --host <tailscale-ip> --port 8090`, open it, go to **Settings**:
   - **Garmin:** paste a Personal API key (Garmin account → API settings). It is checked with Garmin, then saved
     to `data/auth/` (mode 0600) and the first sync starts.
   - **COROS:** click *Log in to COROS*, allow access, then paste the address the tab ends on
     (`http://127.0.0.1:8765/callback?code=…`, the page itself does not load: COROS only allows loopback
     addresses for plain-http apps). Tokens refresh by themselves. On the same machine, `coach login` works too.
   - **Training focus:** road cycling or triathlon. New Claude conversations pick it up.

   Switching the data source clears the cached activities and health data and re-syncs from the new source;
   plan, profile, thresholds and coach note stay.
4. **First backfill** runs from the Settings tab or with `coach sync` (365 days of activities; health 120-365 days;
   FIT files: Garmin 100 per run, COROS 40 per day of its ~50/day allowance).
5. **Start Claude Code in this folder** and say hi. The first session runs a short intake (events, hours,
   thresholds, LTHR).

## Use

| Command | What it does |
|---|---|
| `/checkin` | Morning readiness, then today's session (or rest), saved on the Today page |
| `/session-review` | Coach review of the latest ride / run / swim (or a given activity id), saved on the session's dashboard page |
| `/week-review` | Weekly review and a day-by-day plan for next week |
| `/training-plan` | Build a block toward an event and save it |

Or ask anything: "is my FTP still 319?", "too much intensity this month?", "plan 12 weeks to my 70.3",
"put Thursday's run on my watch" (COROS only; Garmin's Personal API is read-only).

## Raspberry Pi deployment

Templated user units in `deploy/pi/`; the instance name is the checkout's folder in `/home/pi`. Per checkout, copy
`deploy/pi/coach.env.example` to `coach.env` and set the name, session prefix, dashboard port and (for an athlete
with their own Claude account) `CLAUDE_CONFIG_DIR`.

```sh
cp deploy/pi/coach-*@.* ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now coach-web@<folder> coach-sync@<folder>.timer coach-update@<folder>.timer coach-remote@<folder>
```

`coach-update` installs changed units by itself (copy + `daemon-reload`, restarts the sync timer).

| Unit | What it does |
|---|---|
| `coach-sync@.timer` | Sync every 10 min (skips if a sync of that athlete is already running) |
| `coach-auto@.service` | After each sync: unattended check-in / session reviews, if `COACH_AUTO` is set (see below) |
| `coach-web@.service` | Dashboard + Settings on the Tailscale address only (no auth: Tailscale is the boundary); applies accepted suggestions |
| `coach-remote@.service` | Claude Code Remote Control for that checkout (claude.ai / the Claude app) |
| `coach-update@.timer` | Every 5 min: `git pull`, reinstall if `pyproject.toml` changed, restart the dashboard |

An athlete with their own Claude account logs in once with their config dir:
`cd ~/<folder> && CLAUDE_CONFIG_DIR=~/.claude-<name> claude` → `/login`, trust the folder, `/exit`.

### Automatic check-in and session reviews

With `COACH_AUTO=checkin,review` in `coach.env`, every sync is followed by `coach auto`, which starts a headless
Claude Code run (`claude -p`) in the checkout when something is due:

- **Check-in** (`/auto-checkin`): once a day from 04:30, as soon as last night's sleep and HRV are synced (at 10:30
  with whatever is there; skipped if you already ran `/checkin` today or after 13:00).
- **Session review** (`/auto-session-review <id>`): for each session from today or yesterday whose FIT file has
  been analyzed. Sessions already there when the automation first runs are not reviewed.

The auto skills know nobody answers: they never ask and write open questions into the coach note. Like the chat
versions, they never change the plan: each change is a **suggestion** on the Today page (and on the session's page)
with *Accept* / *Dismiss*. *Accept* starts a headless `/apply-suggestion` run from the dashboard service that makes
the change and, with COROS, updates the workout on the watch; the card shows the result. Thresholds, profile, race
and nutrition plans stay chat-only (enforced with `--allowedTools` / `--disallowedTools`). Check-ins show on the Today page and reviews on the
session's page (each reply also in `data/auto/`), the coach note shows the verdict on the dashboard, and a later chat (Remote Control) reads the
report when you ask about it. A run that fails is retried once. Each run uses the athlete's Claude plan
(`CLAUDE_CONFIG_DIR`). Test with `coach auto --dry-run`, or force one kind with `coach auto --checkin` /
`--review`. Logs: `journalctl --user -u coach-auto@<folder>`.

## Notes

- Thresholds: `athlete.json` > values the watch stored in the FIT > the source's profile (Garmin FTP / LTHR /
  max HR, COROS EvoLab threshold pace). Without an LTHR, hrTSS uses 89% of max HR (`tss_source: hr_est`).
  Changing a threshold via Claude recomputes TSS; LTHR / max HR changes also re-read the FIT files for HR zones.
- The Settings tab only accepts JSON requests from its own page (no cross-site form posts); keep the dashboard on
  Tailscale, it has no login.
- Caches from the earlier separate Garmin and COROS coaches (`data/garmin.db`, `data/coros.db`) are adopted and
  migrated automatically on first start.
- If a value shows as empty, run `coach probe` and compare `data/samples/` with the parsers.
