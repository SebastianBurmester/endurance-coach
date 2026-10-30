---
name: checkin
description: Morning coaching check-in. Assesses readiness from HRV, resting HR, sleep, the watch's recovery gauge (Body Battery or COROS recovery) and training load, and decides today's session against the saved plan. Use when the athlete asks "how am I today", "should I train", "what should I ride / run today" or types /checkin.
---

# Morning check-in

1. Call `daily_readiness` and `training_load` (days=28) in parallel. `daily_readiness` includes
   `planned_today` and `planned_next_3_days` from the saved plan. If the profile has upcoming events, also call
   `get_athlete_profile`. If today's session is 90 min or longer, or the athlete has a weight target, also call
   `nutrition_overview` (its `days[0]` is today's carbs, protein and in-session fueling).
2. Decide today's session(s) using the decision rules in CLAUDE.md and the focus guide:
   - **Start from the planned session(s).** Keep (green), modify (amber) or replace with rest or easy recovery (red).
     If nothing is planned, propose a session that fits the current phase, load and (triathlon) the discipline
     that needs it most.
   - Weigh the **flags** over the colour. A single low-HRV night after a hard day is expected.
   - Consider TSB, the last 3 days of TSS (per sport), what was done yesterday (and its notes / RPE), and the
     upcoming planned days. If you move a key session, put it on a later day rather than dropping it silently.
   - Triathlon: running is the highest-impact discipline, so with amber flags cut run intensity first.
   - If an A-event is ≤14 days away, apply taper logic.
3. **Suggest, don't change.** If the decision changes the plan, save one `suggest_plan_change` per session changed
   (source `checkin`, reason = the readiness flags, `changes` = the new session's fields), including a key session
   moved to another day (one suggestion for each day). The plan stays as it is until the athlete accepts: on the
   dashboard (an agent then applies it, also on the COROS calendar) or by saying yes in this chat, in which case
   apply it yourself with `adjust_planned_session` and `resolve_suggestion(id, "applied", …)`.
   Check `open_suggestions` in `daily_readiness` (e.g. from last night's review): build on them, and close one that
   today's numbers make wrong with `resolve_suggestion(id, "superseded", why)`.
4. Reply in this shape (short, no preamble, in the athlete's language):

   **Today: <verdict in one line>** (status: green/amber/red · plan: kept / change suggested / no plan)

   - Recovery: HRV <last night> (<watch status>, 7d <avg> vs normal <range>), RHR <x> (<±delta>),
     sleep <h> h / score <s>, <Body Battery at wake | COROS recovery %>
   - Load: CTL <ctl> · ATL <atl> · TSB <tsb> · ramp <r>/wk
   - Why: <1-2 sentences linking the numbers to the decision>

   Then the session as a workout table in the focus guide's format, plus fueling (g/h during, the day's carbs,
   and whether today is a deficit-OK or fuel-up day when a weight target is set).
   On a rest day, give recovery guidance instead.
5. Save the full reply (as written in step 4, workout table included) with `save_checkin`: it shows on the dashboard's
   Today page. If `daily_readiness` returned a `checkin` for today (e.g. the automatic one), this replaces it, so
   carry over its open points. Then post the result to the dashboard with `set_coach_note` (kind `checkin`): the
   verdict line plus the session in one or two short lines (e.g. "Amber: HRV below range 3 nights. Suggested: today
   75 min Z2 <200 W, VO2 to Thu.").
6. If data is missing (`data_present: false`, `sync_warning`, or no HRV for last night), say what is missing and
   give a conservative recommendation based on load and the plan.
