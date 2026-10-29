---
name: week-review
description: Weekly training review and plan for next week. Covers plan compliance, load progression, intensity distribution, recovery trends and progress toward goals, then saves a detailed day-by-day plan. Use when the athlete asks for a weekly review, "plan my week", "how was my week", or types /week-review.
---

# Weekly review and plan

1. Gather in parallel: `week_summary` (weeks_ago=1 if today is Monday or Tuesday, else 0, and say which week you
   reviewed), `training_load` (days=84), `zone_distribution` (days=28), `health_trends` (days=28),
   `get_athlete_profile`, and `get_plan` (start_date=next Monday, days=14).
2. Review:
   - **Plan compliance** (`plan_vs_actual`): completion %, planned vs actual TSS and hours, missed or partial key
     sessions (incl. "<sport> missing" days), intensity missed, unplanned sessions, and adjustments made during the
     week (`originally`, `change_reason`).
   - Volume (per sport for triathlon) and TSS vs the previous 3 weeks. CTL ramp: sustainable (3-6/wk), too steep
     or flat? Triathlon: run volume jump >10%?
   - Intensity distribution: share of easy time, grey-zone creep, polarization index.
   - Recovery: HRV 7-day trend vs normal range, RHR trend, sleep average, the watch's recovery gauge. Any warning
     pattern? Session notes (illness, pain, cycle) as context.
   - Where we are in the block / season relative to the next A/B event (weeks to go → phase).
   - Weight (`nutrition_overview`, if a target is set or weigh-ins exist): trend and 4-week rate vs target and the
     safe rate, long sessions with late drift (fueling), any findings.
3. Plan next week:
   - Start from what is already planned (`get_plan`), carrying forward or dropping missed key sessions. Lower the
     load if recovery markers are down or compliance was poor because of fatigue. A recovery week comes every
     3rd-4th week.
   - Target weekly TSS and hours from current CTL (roughly CTL×7 holds fitness; +5-10% builds), within
     `weekly_hours_available` (split across swim / bike / run for triathlon).
   - Use the athlete's long-session days. Max 2-3 hard sessions, never back to back unless it is a block.
   - Show a day-by-day table: day | sport | session | duration | key targets (watts / pace / HR) | TSS.
   - Once agreed, save the week with `save_plan`, every day included (rest = `sport: rest`), with full `targets` and
     `structure` for each session. Check the `projection` in the result.
   - COROS connected: offer to push next week's run and bike sessions to the watch (`push_session_to_coros`).
   - Give the 1-2 priorities for the week and what would trigger an adjustment (e.g. HRV below range 2 days in a row).
   - With a weight target: name the fuel-up days (key and long sessions) and the deficit-OK days of the new week.
4. Post to the dashboard with `set_coach_note` (kind `week-review`): last week's verdict and compliance, and the
   1-2 priorities for the coming week, in 2-4 short lines.
5. Keep it scannable: verdict → compliance line → 4-6 review bullets → plan table → priorities.
