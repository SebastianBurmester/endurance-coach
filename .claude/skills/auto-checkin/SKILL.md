---
name: auto-checkin
description: Unattended morning check-in, started by the Pi's timer (`coach auto`) once the night's sleep data has synced. Nobody reads the session while it runs and nobody can answer. Not for conversations with the athlete: there, use /checkin.
disable-model-invocation: true
---

# Unattended morning check-in

**This is an automated run. No one is in this session while it runs.** Never wait for confirmation and do not end
with "shall I…?" or "let me know". Decide yourself, take the conservative option when unsure, and state the
assumption in one short clause. Questions go to the dashboard with `ask_athlete` (step 4), never into the reply as
something to wait for.

1. Read `.claude/skills/checkin/SKILL.md` and follow its steps 1-6, with the changes below.
2. **Plan changes are suggestions only** (`suggest_plan_change`, `origin: "auto"`), as step 3 of that skill says:
   at most today and the next 3 days, only for readiness reasons. The athlete accepts them on the dashboard, and the
   run that applies them also updates the COROS calendar. Never rebuild the plan, never change thresholds, weight,
   profile, race or nutrition plans (changing tools are blocked in this run).
3. **Open suggestions** from last night's review are in `daily_readiness.open_suggestions`: if this morning's numbers
   change the picture, supersede them (`resolve_suggestion`) with a reason, or leave them for the athlete.
4. **Things that need the athlete** (a symptom question, a threshold that looks off, a missing intake answer, data
   that looks wrong): ask with `ask_athlete`, one question per call, at most 2 per run, and only when the answer
   could change a decision. Give 2-4 short quick-reply `options` in the athlete's language when the answer is a
   choice (e.g. "Halsschmerzen oder Infekt?" → "Nein, alles gut" / "Leicht kratzig" / "Ja, krank"). Do not wait:
   decide now on the conservative assumption. Also list them as one line "Frage an dich: …" / "Question for you: …"
   at the end of the reply. The athlete answers on the dashboard, and the answer comes back into this session as a
   chat message: then update the check-in, coach note and suggestions if it changes anything.
5. **Missing data:** if last night's sleep or HRV is missing, say so and decide conservatively from load and the
   plan (amber at most for intensity), as the check-in skill describes.
6. **Language:** the athlete is not here to write first. Use the language of their recent workout notes
   (`athlete_note` in `list_activities` / `analyze_session`); without notes, use English.
7. **Save the check-in** with `save_checkin` (`origin: "auto"`, the full final reply including the open points): the
   athlete reads it on the dashboard's Today page. If `daily_readiness` already returned a `checkin` for today, the
   athlete checked in in a chat: reply with one line saying so and save nothing (no check-in, no coach note, no
   questions).
8. **Coach note is mandatory:** always end with `set_coach_note` (kind `checkin`), starting with "Auto check-in:".
   It is what the athlete sees first on the dashboard.
9. Your final message is also saved as the report in `data/auto/` and read later; write it in the check-in skill's
   reply shape, complete on its own.
