---
name: auto-session-review
description: Unattended review of one newly synced session (argument: activity id), started by the Pi's timer (`coach auto`) when its FIT file has been analyzed. Nobody reads the session while it runs and nobody can answer. Not for conversations with the athlete: there, use /session-review.
disable-model-invocation: true
---

# Unattended session review

**This is an automated run. No one is in this session while it runs.** Never wait for confirmation and do not end
with "shall I…?" or "let me know". Where the interactive skill says "ask", put the question on the dashboard with
`ask_athlete` instead (step 4) and carry on.

1. Read `.claude/skills/session-review/SKILL.md` and follow its steps for the activity id given as the argument
   (`analyze_session(activity_id=…)`). Without an id, review the latest session.
2. **Plan changes are suggestions only**, as step 5 of that skill says (`suggest_plan_change`, `origin: "auto"`,
   the activity id; title e.g. "Do: Intervalle → 60 min Z2" / "Thu: intervals → 60 min Z2"). The athlete accepts
   them on the dashboard; the next check-in sees them too. Mention them in the reply and the coach note.
3. **Thresholds:** if the session suggests a new FTP, threshold pace, CSS or LTHR, name the number and the evidence
   as a proposal. Never set it (the profile tools are blocked in this run).
4. **Things that need the athlete** (how it felt when there is no note or RPE, pain, a session that looks
   mis-recorded): ask with `ask_athlete` (with the activity id), one question per call, at most 2 per run, with 2-4
   short quick-reply `options` in the athlete's language when the answer is a choice (e.g. RPE: "Locker (3-4)" /
   "Mittel (5-6)" / "Hart (7-8)" / "Am Limit (9-10)"). Do not wait: review on the data you have. Also list them as
   one line "Frage an dich: …" / "Question for you: …" at the end of the reply. The answer comes back into this
   session as a chat message: then update the review and coach note if it changes the verdict.
5. **Language:** use the language of the session's `athlete_note`, else of recent workout notes; without notes,
   English.
6. **Save the review on the session** with `save_session_review` (`origin: "auto"`, the full final reply including
   the open points and proposals): this is where the athlete reads it, on the session's dashboard page.
7. **Coach note is mandatory:** always end with `set_coach_note` (kind `session-review`), starting with
   "Auto review <sport> <date>:". The note replaces the previous one: if `daily_readiness` returns today's
   `checkin`, keep its verdict line at the top of the new note so it is not lost.
8. Your final message is also saved as the report in `data/auto/` and read later; write it in the review skill's
   format, complete on its own.
