"""The dashboard's chat and the coach's questions.

Every conversation is a Claude Code session in this checkout, run headless: the first message starts it with
`claude -p --session-id <id>`, every later one resumes it with `--resume <id>`. The automatic check-in and session
reviews are conversations too (auto.py gives them a session id), so the athlete can answer them from the dashboard.
What the coach asks with `ask_athlete` shows on the dashboard with quick replies; an answer goes back into the
session that asked (COACH_CHAT_ID tells the MCP server which one that is).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import uuid
from datetime import date, datetime, timedelta
from typing import Any

from . import store
from .config import ROOT

_LOGGER = logging.getLogger(__name__)
KINDS = ("chat", "checkin", "session-review")
MAX_TEXT = 8000
TURN_TIMEOUT_S = 15 * 60
QUESTION_DAYS = 3           # open questions stay on the dashboard this long
_TURNS = threading.BoundedSemaphore(2)  # Claude processes the dashboard runs at once (the Pi has little memory)

# The athlete is in the conversation: all coach tools (plan, thresholds, weight only after they confirm, as
# CLAUDE.md says), but nothing that touches the shell or files. AskUserQuestion has no UI here: ask_athlete has.
_ALLOWED = ["mcp__coach", "Skill", "Read", "Glob", "Grep", "WebSearch"]
_NEVER = ["AskUserQuestion", "Bash", "PowerShell", "Edit", "Write", "NotebookEdit", "WebFetch"]

_BRIEF = (
    "The athlete is writing to you from the chat page of the coach dashboard and reads your replies there (often on "
    "a phone). This is a normal coaching conversation: CLAUDE.md's rules for chats apply. AskUserQuestion is not "
    "available: to offer choices, call ask_athlete with the question and 2-5 short options, then end your turn; "
    "the answer arrives as the next message. Replies render as markdown (bold, bullets, tables): keep them compact.")
_REPLY_TO_AUTO = (
    "[The athlete has now answered on the dashboard. From here on this is a conversation, not an unattended run: "
    "the athlete is here and reads your replies. " + _BRIEF + " If the answer changes the check-in or review you "
    "wrote, update it (save_checkin / save_session_review with origin chat, set_coach_note, suggestions; a plan "
    "change they ask for directly is made right away).]")


def _now() -> str:
    return store._now()


def _chat(chat_id: str) -> dict[str, Any] | None:
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return dict(row) if row else None


def _update(chat_id: str, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with store.connect() as conn:
        conn.execute(f"UPDATE chats SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), _now(), chat_id))


def create(kind: str, title: str, chat_id: str | None = None, activity_id: int | None = None,
           day: str | None = None, runner: str = "web", status: str = "idle") -> str:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    chat_id = chat_id or str(uuid.uuid4())
    now = _now()
    with store.connect() as conn:
        conn.execute("INSERT INTO chats(id, kind, title, activity_id, day, status, runner, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (chat_id, kind, title.strip()[:120] or "Chat", activity_id, day, status, runner, now, now))
    return chat_id


def _add_message(chat_id: str, role: str, text: str, question: str | None = None) -> None:
    with store.connect() as conn:
        conn.execute("INSERT INTO chat_messages(chat_id, role, text, question, created_at) VALUES (?, ?, ?, ?, ?)",
                     (chat_id, role, text.strip(), question, _now()))


def _question_row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["options"] = json.loads(d.pop("options_json") or "[]")
    d["multi"] = bool(d["multi"])
    return d


def get(chat_id: str) -> dict[str, Any]:
    chat = _chat(chat_id)
    if chat is None:
        raise ValueError("no such chat")
    with store.connect() as conn:
        msgs = [dict(r) for r in conn.execute(
            "SELECT id, role, text, question, created_at FROM chat_messages WHERE chat_id = ? ORDER BY id", (chat_id,))]
        qs = [_question_row(r) for r in conn.execute(
            "SELECT * FROM questions WHERE chat_id = ? ORDER BY id", (chat_id,))]
    return {"chat": chat, "messages": msgs, "questions": qs}


def listing(limit: int = 50) -> list[dict[str, Any]]:
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT c.id, c.kind, c.title, c.activity_id, c.day, c.status, c.updated_at, "
            "(SELECT text FROM chat_messages m WHERE m.chat_id = c.id ORDER BY m.id DESC LIMIT 1) AS last, "
            "(SELECT COUNT(*) FROM questions q WHERE q.chat_id = c.id AND q.status = 'open') AS open_questions "
            "FROM chats c ORDER BY c.updated_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["last"] = " ".join((d["last"] or "").split())[:140]
        out.append(d)
    return out


# ---------------------------------------------------------------- questions (ask_athlete)

def ask(question: str, options: list[str] | None = None, multi: bool = False, source: str | None = None,
        activity_id: int | None = None) -> dict[str, Any]:
    """Save a question for the dashboard. Called by the MCP server inside a Claude session."""
    chat_id = os.environ.get("COACH_CHAT_ID") or None
    chat = _chat(chat_id) if chat_id else None
    if chat_id and chat is None:  # an automatic run's chat is created before the run; keep the link anyway
        _LOGGER.warning("question for unknown chat %s", chat_id)
    source = source or (chat["kind"] if chat else "chat")
    if source not in KINDS:
        raise ValueError(f"source must be one of {KINDS}")
    opts = [o.strip()[:80] for o in options or [] if o and o.strip()][:6]
    activity_id = activity_id if activity_id is not None else (chat or {}).get("activity_id")
    now = _now()
    with store.connect() as conn:
        cur = conn.execute(
            "INSERT INTO questions(chat_id, question, options_json, multi, source, activity_id, status, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (chat_id, question.strip(), json.dumps(opts), int(bool(multi and opts)), source, activity_id, now, now))
    note = ("Shown on the dashboard with the options as quick replies and a link to this chat. Do not wait for it: "
            "finish your turn. The answer arrives as the next message in this conversation." if chat_id else
            "Shown on the dashboard; the answer starts a new dashboard chat (this conversation is not one). If the "
            "athlete is here in this conversation, ask in your reply instead.")
    return {"question_id": cur.lastrowid, "note": note}


def open_questions(today: date | None = None) -> list[dict[str, Any]]:
    since = ((today or date.today()) - timedelta(days=QUESTION_DAYS)).isoformat()
    with store.connect() as conn:
        rows = conn.execute("SELECT q.*, c.title AS chat_title, c.status AS chat_status FROM questions q "
                            "LEFT JOIN chats c ON c.id = q.chat_id WHERE q.status = 'open' AND q.created_at >= ? "
                            "ORDER BY q.id", (since,)).fetchall()
    return [_question_row(r) for r in rows]


def _question(qid: int) -> dict[str, Any]:
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
    if row is None:
        raise ValueError(f"no question {qid}")
    return _question_row(row)


def answer_question(qid: int, answer: str) -> dict[str, Any]:
    """A quick reply from the dashboard: it goes to the session that asked, or to a new chat."""
    q = _question(qid)
    if q["status"] != "open":
        raise ValueError("this question was already answered")
    answer = answer.strip()
    if not answer:
        raise ValueError("empty answer")
    if q["chat_id"] and _chat(q["chat_id"]):
        chat_id = q["chat_id"]
        prompt = f'(Answer to your question "{q["question"]}")\n{answer}'
    else:
        chat_id = create(q["source"] if q["source"] in KINDS else "chat", q["question"][:80],
                         activity_id=q["activity_id"])
        what = {"checkin": "this morning's check-in", "session-review": "a session review"}.get(q["source"], "an earlier conversation")
        prompt = (f'In {what} you asked me on the dashboard: "{q["question"]}"\nMy answer: {answer}'
                  + (f"\n(activity id {q['activity_id']})" if q["activity_id"] else ""))
    result = send(chat_id, answer, prompt=prompt, question=q)
    return result


def dismiss_question(qid: int) -> dict[str, Any]:
    _question(qid)
    with store.connect() as conn:
        conn.execute("UPDATE questions SET status = 'dismissed', updated_at = ? WHERE id = ? AND status = 'open'",
                     (_now(), qid))
    return {"questions": open_questions()}


# ---------------------------------------------------------------- a turn: one claude -p run

def send(chat_id: str | None, text: str, prompt: str | None = None,
         question: dict[str, Any] | None = None) -> dict[str, Any]:
    """The athlete's message: saved, then a background turn relays it to the Claude session."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty message")
    if len(text) > MAX_TEXT:
        raise ValueError(f"message too long (max {MAX_TEXT} characters)")
    if chat_id is None:
        chat_id = create("chat", text.splitlines()[0][:80])
    chat = _chat(chat_id)
    if chat is None:
        raise ValueError("no such chat")
    with store.connect() as conn:
        cur = conn.execute("UPDATE chats SET status = 'running', runner = 'web', progress = NULL, draft = NULL, "
                           "error = NULL, updated_at = ? WHERE id = ? AND status != 'running'", (_now(), chat_id))
        if cur.rowcount == 0:
            raise ValueError("the coach is still replying; wait a moment")
        first = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE chat_id = ? AND role = 'athlete'",
                             (chat_id,)).fetchone()[0] == 0
        if question:
            conn.execute("UPDATE questions SET status = 'answered', answer = ?, updated_at = ? WHERE id = ?",
                         (text, _now(), question["id"]))
        else:  # writing in the chat answers what is still open there
            conn.execute("UPDATE questions SET status = 'answered', answer = ?, updated_at = ? "
                         "WHERE chat_id = ? AND status = 'open'", ("(in the chat)", _now(), chat_id))
    _add_message(chat_id, "athlete", text, question["question"] if question else None)
    prompt = prompt or text
    if first and chat["kind"] != "chat" and not prompt.startswith("/"):
        prompt = f"{_REPLY_TO_AUTO}\n\n{prompt}"
    threading.Thread(target=_turn, args=(chat_id, prompt), daemon=True, name=f"chat-{chat_id[:8]}").start()
    return {"chat_id": chat_id, **get(chat_id)}


def retry(chat_id: str) -> dict[str, Any]:
    chat = _chat(chat_id)
    if chat is None or chat["status"] != "failed":
        raise ValueError("nothing to retry")
    with store.connect() as conn:
        last = conn.execute("SELECT id, text, question FROM chat_messages WHERE chat_id = ? AND role = 'athlete' "
                            "ORDER BY id DESC LIMIT 1", (chat_id,)).fetchone()
        if last is None:
            raise ValueError("nothing to retry")
        conn.execute("DELETE FROM chat_messages WHERE id = ?", (last["id"],))
        conn.execute("UPDATE chats SET status = 'idle' WHERE id = ?", (chat_id,))
    q = None
    if last["question"]:
        prompt = f'(Answer to your question "{last["question"]}")\n{last["text"]}'
        with store.connect() as conn:  # keep the "answers: …" line on the message
            row = conn.execute("SELECT * FROM questions WHERE chat_id = ? AND question = ? ORDER BY id DESC LIMIT 1",
                               (chat_id, last["question"])).fetchone()
        q = _question_row(row) if row else {"id": -1, "question": last["question"]}
    else:
        prompt = None
    return send(chat_id, last["text"], prompt=prompt, question=q)


def _tool_label(block: dict[str, Any]) -> str:
    name = str(block.get("name") or "")
    inp = block.get("input") or {}
    if name == "Skill":
        return f"skill {inp.get('skill') or inp.get('command') or ''}".strip()
    return name.removeprefix("mcp__coach__").replace("_", " ")


def _command(chat: dict[str, Any], prompt: str) -> list[str]:
    from . import auto

    cmd = [auto.claude_path(), "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--allowedTools", ",".join(_ALLOWED), "--disallowedTools", ",".join(_NEVER)]
    if chat["started"]:
        cmd += ["--resume", chat["id"]]
    else:
        cmd += ["--session-id", chat["id"], "--append-system-prompt", _BRIEF]
    if os.environ.get("COACH_CHAT_MODEL"):
        cmd += ["--model", os.environ["COACH_CHAT_MODEL"]]
    return cmd


def _turn(chat_id: str, prompt: str) -> None:
    with _TURNS:
        chat = _chat(chat_id)
        if chat is None:
            return
        texts: list[str] = []
        result: str | None = None
        error: str | None = None
        try:
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as err:
                proc = subprocess.Popen(_command(chat, prompt), cwd=ROOT, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8",
                                        env={**os.environ, "COACH_CHAT_ID": chat_id})
                timed_out = threading.Event()
                timer = threading.Timer(TURN_TIMEOUT_S, lambda: (timed_out.set(), proc.kill()))
                timer.start()
                try:
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        kind = ev.get("type")
                        if kind == "system" and ev.get("subtype") == "init":
                            _update(chat_id, started=1)
                        elif kind == "assistant":
                            for block in (ev.get("message") or {}).get("content") or []:
                                if block.get("type") == "text" and block.get("text", "").strip():
                                    texts.append(block["text"].strip())
                                    _update(chat_id, draft=texts[-1][:4000], progress=None)
                                elif block.get("type") == "tool_use":
                                    _update(chat_id, progress=_tool_label(block)[:80])
                        elif kind == "result":
                            result = ev.get("result")
                            if ev.get("is_error"):
                                error = str(result or ev.get("subtype") or "the run failed")
                    proc.wait()
                finally:
                    timer.cancel()
                err.seek(0)
                stderr = err.read().strip()
            if timed_out.is_set():
                error = f"no reply within {TURN_TIMEOUT_S // 60} min"
            elif proc.returncode != 0 and not error:
                error = stderr[-400:] or f"claude exited with {proc.returncode}"
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("chat turn failed")
            error = str(exc)
        reply = (result or "").strip() or "\n\n".join(texts)
        if error or not reply:
            _update(chat_id, status="failed", progress=None, error=(error or "no reply")[:500])
            return
        _add_message(chat_id, "coach", reply)
        _update(chat_id, status="idle", progress=None, draft=None, error=None)


# ---------------------------------------------------------------- automatic runs and restarts

def begin_auto(kind: str, title: str, activity_id: int | None = None, day: str | None = None) -> str:
    """An automatic check-in / review starts its session under this id (coach auto runs it, not the dashboard)."""
    return create(kind, title, activity_id=activity_id, day=day, runner="auto", status="running")


def finish_auto(chat_id: str, ok: bool, reply: str) -> None:
    if ok and reply.strip():
        _add_message(chat_id, "coach", reply)
        _update(chat_id, status="idle", started=1, progress=None, draft=None)
    else:  # the next try starts a fresh session; the report in data/auto/ keeps what happened
        with store.connect() as conn:
            conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))


def resume() -> None:
    """On dashboard start: turns cut off by a restart can be sent again."""
    with store.connect() as conn:
        conn.execute("UPDATE chats SET status = 'failed', progress = NULL, error = 'interrupted: the dashboard "
                     "restarted', updated_at = ? WHERE status = 'running' AND runner = 'web'", (_now(),))
        # a coach auto run that died without finishing
        stale = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM chats WHERE status = 'running' AND runner = 'auto' AND updated_at < ?", (stale,))
