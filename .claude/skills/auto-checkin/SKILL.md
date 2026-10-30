---
name: auto-checkin
description: Unattended morning check-in, started by the Pi's timer (`coach auto`) once the night's sleep data has synced. Nobody reads the session while it runs and nobody can answer. Not for conversations with the athlete: there, use /checkin.
disable-model-invocation: true
---

# Unattended morning check-in

**This is an automated run. No one is in this session and no one will answer.** Never ask a question, offer a
choice, or wait for confirmation; do not end with "shall I…?" or "let me know". Decide yourself, take the
conservative option when unsure, and state the assumption in one short clause.

1. Read `.claude/skills/checkin/SKILL.md` and follow its steps 1-6, with the changes below.
2. **Plan changes are suggestions only** (`suggest_plan_change`, `origin: "auto"`), as step 3 of that skill says:
   at most today and the next 3 days, only for readiness reasons. The athlete accepts them on the dashboard, and the
   run that applies them also updates the COROS calendar. Never rebuild the plan, never change thresholds, weight,
   profile, race or nutrition plans (changing tools are blocked in this run).
3. **Open suggestions** from last night's review are in `daily_readiness.open_suggestions`: if this morning's numbers
   change the picture, supersede them (`resolve_suggestion`) with a reason, or leave them for the athlete.
4. **Things that need the athlete** (a symptom question, a threshold that looks off, a missing intake answer, data
   that looks wrong): do not ask. Add them as one line "Für dich offen: …" / "Open for you: …" at the end of the
   reply and the coach note, so the athlete can answer in the next chat.
5. **Missing data:** if last night's sleep or HRV is missing, say so and decide conservatively from load and the
   plan (amber at most for intensity), as the check-in skill describes.
6. **Language:** the athlete is not here to write first. Use the language of their recent workout notes
   (`athlete_note` in `list_activities` / `analyze_session`); without notes, use English.
7. **Save the check-in** with `save_checkin` (`origin: "auto"`, the full final reply including the open points): the
   athlete reads it on the dashboard's Today page. If `daily_readiness` already returned a `checkin` for today, the
   athlete checked in in a chat: reply with one line saying so and save nothing (no check-in, no coach note).
8. **Coach note is mandatory:** always end with `set_coach_note` (kind `checkin`), starting with "Auto check-in:".
   It is what the athlete sees first on the dashboard.
9. Your final message is also saved as the report in `data/auto/` and read later; write it in the check-in skill's
   reply shape, complete on its own.
