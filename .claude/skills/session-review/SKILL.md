---
name: session-review
description: Coach-style review of a ride, run, swim or brick (default the latest session) against the planned session. Covers execution vs plan, pacing, power / pace / HR zones, decoupling, laps, best efforts and fueling cues. Use when the athlete says "review my ride", "how did my run / swim go", shares an activity id, or types /session-review (or /ride-review).
---

# Session review

1. Call `analyze_session` (pass the activity id if one was given, or `sport` if the athlete names one: "my ride"
   → `bike`, "my run" → `run`). With the road-cycling focus and no sport named, review the latest ride
   (`sport: bike`). Its `planned_session` field holds what was planned that day (with `originally` if it was
   adjusted). Without a plan, infer the purpose from the name, IF, VI, the laps and the watch's training focus.
   Read the `athlete_note` and `perceived_effort`: they matter (illness, cycle, heat, how it felt).
2. Assess the session against the plan or purpose:
   - **Ride intervals**: were the work laps on the planned targets (watts / % FTP, or HR without power)? Did power
     fade across reps (last vs first)? Did HR still rise across reps at the same power?
   - **Endurance / long ride**: IF 0.60-0.75? Time in Z3+ (grey-zone creep)? Pw:HR decoupling on a steady ride
     <5%? Any late fade that suggests under-fueling?
   - **Race / group ride**: VI, best efforts (new 90-day bests?), where the matches were burned.
   - **Run intervals**: work laps on target pace (`pct_threshold_pace`, grade-adjusted pace)? Pace fade or HR
     climb across reps? Recovery jogs easy enough?
   - **Easy / long run**: HR in Z1-Z2? NGP sensible for the HR? Decoupling <5% on steady runs >60 min? Cadence?
   - **Swim**: pace per 100 m vs CSS on the main set (laps), consistency across reps. Ignore wrist HR.
   - **Brick**: run pace off the bike vs target, how quickly the athlete settled (first km vs the rest).
   - **Planned vs done**: duration and TSS vs plan. If the athlete did something else, say so plainly and note the
     knock-on effect on the next planned days.
3. Note threshold signals: new 90-day power bests or a 20-min / CP estimate >3% above FTP (`power_curve`), or a run
   clearly faster than threshold pace at similar HR. Propose a value; never set it without confirmation.
4. Comment on cadence / coasting and HR vs output, comparing EF with similar recent sessions via `list_activities`
   when useful.
5. If the session makes upcoming planned sessions unwise (much harder than planned, very high decoupling, signs of
   illness or pain), propose the change and, once agreed, save it with `adjust_planned_session`.
6. Post the verdict and the takeaway to the dashboard with `set_coach_note` (kind `session-review`), 1-3 short lines.
7. Format: a 1-line verdict (incl. "as planned" / "off plan"), a compact key-numbers line (duration, distance, NP /
   W/kg or pace, avg HR, TSS + source, IF, decoupling), 2-4 bullets on what went well and what to change, and
   1 concrete takeaway for the next similar session.
