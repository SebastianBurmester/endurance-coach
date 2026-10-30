---
name: apply-suggestion
description: Unattended run that applies one suggested plan change (argument: suggestion id) after the athlete pressed Accept on the dashboard. Nobody reads the session while it runs and nobody can answer. Not for conversations with the athlete.
disable-model-invocation: true
---

# Apply an accepted suggestion

**This is an automated run. No one is in this session and no one will answer.** Never ask a question or wait for
confirmation. The athlete already agreed by pressing Accept on the dashboard.

1. `list_suggestions(suggestion_id=…)` with the id given as the argument. Only continue if its status is
   `applying`; otherwise stop with one line.
2. `get_plan` from the suggestion's date (7 days) to see the session as it stands now, and `daily_readiness` if
   the change is for today.
3. **Still fits?** If the day is past, the session was already done, or the plan for that day changed since the
   suggestion (it no longer matches what the suggestion describes), do not apply it:
   `resolve_suggestion(id, "failed", "<why, one line>")` and stop.
4. **Apply** with `adjust_planned_session` for that date and slot, starting from `changes` and `title`. Reason:
   "accepted on the dashboard: <the suggestion's reason>". If the change needs a new workout, write the full
   `structure` and `targets` in the focus guide's workout format, using the athlete's thresholds (`get_athlete_profile`).
   Change only what the suggestion says: other days only when the suggestion explicitly moves a session there.
5. **COROS calendar** (COROS only): if the changed session has a `coros_ref` and is a run or bike, read
   `coros_workout_schema` and push the new version with `push_session_to_coros` (same day and slot) so the watch
   matches. If it became a rest day, swim or skip, the old workout stays on the watch: say in the result that it has
   to be deleted in the COROS app.
6. `resolve_suggestion(id, "applied", "<what changed, one line, e.g. 'Thu: VO2 → 60 min Z2, updated on COROS'>")`.
7. Update the coach note with `set_coach_note` (kind `plan`): keep the first line of the current verdict if it is
   from today's check-in (`daily_readiness.checkin`), then "Applied: <the result line>".
8. Reply with the result line only.
