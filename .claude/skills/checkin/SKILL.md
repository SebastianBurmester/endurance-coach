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
3. If the decision changes the plan, save it with `adjust_planned_session` (reason = the readiness flags), including
   any key session moved to another day. If a changed session is already on the COROS calendar (`coros_ref`), offer
   to push the new version (`push_session_to_coros`) or tell the athlete to delete it in the COROS app. Tell the
   athlete what changed.
4. Reply in this shape (short, no preamble, in the athlete's language):

   **Today: <verdict in one line>** (status: green/amber/red · plan: kept / adjusted / no plan)

   - Recovery: HRV <last night> (<watch status>, 7d <avg> vs normal <range>), RHR <x> (<±delta>),
     sleep <h> h / score <s>, <Body Battery at wake | COROS recovery %>
   - Load: CTL <ctl> · ATL <atl> · TSB <tsb> · ramp <r>/wk
   - Why: <1-2 sentences linking the numbers to the decision>

   Then the session as a workout table in the focus guide's format, plus fueling (g/h during, the day's carbs,
   and whether today is a deficit-OK or fuel-up day when a weight target is set).
   On a rest day, give recovery guidance instead.
5. Post the result to the dashboard with `set_coach_note` (kind `checkin`): the verdict line plus the session in
   one or two short lines (e.g. "Amber: HRV below range 3 nights. Today 75 min Z2 <200 W; VO2 moved to Thu.").
6. If data is missing (`data_present: false`, `sync_warning`, or no HRV for last night), say what is missing and
   give a conservative recommendation based on load and the plan.
