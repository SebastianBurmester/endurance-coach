---
name: training-plan
description: Build or rebuild a training plan or block (e.g. "plan the next 12 weeks for my 70.3", "plan 8 weeks for the Marmotte", "build me a base block", "re-plan next week") and save it so check-ins and reviews follow it. Use when the athlete asks for a training plan, block or schedule, or types /training-plan.
---

# Build a training plan

1. Gather context in parallel: `get_athlete_profile`, `training_load` (days=84), `health_trends` (days=28),
   `zone_distribution` (days=42), `get_plan` (start_date=today, days=28) to see what is already planned.
2. If anything essential is missing, ask once (max 4 questions): target event, format and date, weekly hours and
   long-session days, trainer / pool / open-water access, constraints (travel, work, injuries). Save new facts with
   `update_athlete_profile`. Thresholds used for targets (FTP, run threshold pace, CSS, LTHR) must be confirmed
   first; until then write HR-zone and RPE targets.
3. Design the plan following the focus guide's training approach:
   - Phases backwards from the A-event: taper (7-14 d) ← peak/specialty ← build ← base. Recovery week every
     3rd-4th week.
   - Weekly TSS from current CTL: about CTL×7 holds fitness, +5-10%/week builds. Keep the CTL ramp at 3-6/week.
   - At most 2-3 hard sessions per week (across all sports), never back to back unless it is a planned block.
     Long sessions on the athlete's long-session days.
   - Road cycling: long ride progressing toward event duration, intensity matched to the event (climbing
     sweet spot / threshold for fondos, VO2 and anaerobic work for road races and crits).
   - Triathlon: split hours across swim / bike / run to fit the race and the weakest discipline, run volume
     +10%/week at most, bricks from build on, intensity matched to the race format.
   - Weight goal (profile `target_weight_kg` or the athlete asks): place the deficit in base, check the needed rate
     in `nutrition_overview` (≤0.5%/week), hold weight from 3 weeks before the A event, and build in-ride carbs
     over the long rides. Save the approach with `save_nutrition_plan`.
4. Show the plan before saving: a block summary (phase, weeks, goal, weekly hours/TSS, per sport for triathlon),
   then week 1-2 in full detail and later weeks as a compact week-by-week outline.
5. After the athlete agrees, save with `save_plan`:
   - One `block` per phase (call save_plan once per block if there are several), and every day as a session,
     including rest days (`sport: rest`, `type: rest`). Two sessions on one day: slot 1 and slot 2.
   - Weeks 1-2 get full detail: `targets` (watts / pace / HR) and `structure` (warm-up, main set, cool-down,
     fueling). Later weeks can have sport, type, title, duration and TSS only. Detail them in /week-review.
   - Set `tss` where you can estimate it. Otherwise it is estimated from type and duration.
6. Check `projection` in the save_plan result: projected TSB on event day should be +10 to +25, and the ramp should
   stay sane. If not, adjust and save again (replace_existing re-writes the range).
7. COROS connected: ask whether the run and bike sessions of the first week should go on the watch. If yes, push
   them (`coros_workout_schema` once, then `push_session_to_coros` per session).
8. Post a 2-3 line summary of the new plan to the dashboard with `set_coach_note` (kind `plan`): block, goal,
   the key change, and what happens this week.
9. End with the next 3 days of sessions and one line on what the plan is built to achieve.
