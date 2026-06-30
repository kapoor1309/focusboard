"""
FocusBoard - a tiny "departures board" for your Claude Code agent.

Why this exists:
    While the agent is thinking, the wait feels unknown, so your brain bails
    to the phone. This little server shows what the agent is doing live - the
    tools it runs AND the answer as it forms - plus a parking lot to dump your
    next thought instead of doom-scrolling.

Two things feed the board:
    1. Hooks  - Claude Code pings us on each event (prompt, tool use, finish).
                These are instant, so the tool feed updates the moment it acts.
    2. The transcript - Claude Code writes a running log file of the whole
                conversation. We watch the current turn's slice of it to pull
                out the phase (thinking / writing / using tools) and the answer
                text as it gets written. This is how the board stays alive even
                on a pure-thinking turn that uses no tools.

Run it:
    python server.py
    then open http://localhost:8137 in a browser tab next to your terminal.

No pip installs needed - this uses only Python's built-in libraries.
"""

import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8137
HERE = os.path.dirname(os.path.abspath(__file__))
NOTES_FILE = os.path.join(HERE, "parking_lot.txt")

# One lock guards STATE because two threads touch it: the web handler (hooks)
# and the background transcript watcher.
LOCK = threading.Lock()

STATE = {
    "status": "idle",        # "idle" | "working" | "done"
    "phase": "idle",         # "idle" | "thinking" | "writing" | "tools" | "done"
    "prompt": "",            # the last thing you asked the agent
    "answer": "",            # the agent's reply text so far this turn
    "started": 0.0,          # epoch seconds when this turn began
    "finished": 0.0,         # epoch seconds when the agent stopped
    "activity": [],          # list of {"label","detail","t"} - the tool feed
    "session": "",           # which Claude Code session this is
    "transcript": "",        # path to the running transcript file
    "turn_offset": 0,        # byte position in that file where this turn began
}

NOTES = []


def _now():
    return time.time()


# ---------------------------------------------------------------------------
# Turning raw tool calls into plain-English feed lines.
# ---------------------------------------------------------------------------
def _friendly(tool_name, tool_input):
    tool_input = tool_input or {}

    def short(path):
        return os.path.basename(str(path)) if path else ""

    if tool_name == "Read":
        return ("Reading", short(tool_input.get("file_path")))
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        return ("Editing", short(tool_input.get("file_path")))
    if tool_name in ("Bash", "PowerShell"):
        cmd = str(tool_input.get("command", "")).strip().replace("\n", " ")
        return ("Running", cmd[:70])
    if tool_name in ("Grep", "Glob"):
        return ("Searching", str(tool_input.get("pattern", "")))
    if tool_name in ("WebFetch", "WebSearch"):
        return ("Looking online", str(tool_input.get("url") or tool_input.get("query", "")))
    if tool_name in ("Agent", "Task"):
        return ("Running a helper agent", str(tool_input.get("description", "")))
    return (tool_name or "Working", "")


# ---------------------------------------------------------------------------
# Finding the transcript file for a session.
# Claude Code stores it at:
#   ~/.claude/projects/<cwd-with-slashes-as-dashes>/<session_id>.jsonl
# We prefer the path the hook hands us, and fall back to building it ourselves.
# ---------------------------------------------------------------------------
def _transcript_path(data):
    p = data.get("transcript_path")
    if p and os.path.exists(p):
        return p
    session = data.get("session_id", "")
    cwd = data.get("cwd", "")
    if not session or not cwd:
        return ""
    proj = re.sub(r"[:\\/]+", "-", cwd)
    guess = os.path.join(os.path.expanduser("~"), ".claude", "projects", proj, session + ".jsonl")
    return guess if os.path.exists(guess) else ""


def _handle_event(data):
    event = data.get("hook_event_name", "")

    with LOCK:
        if event == "UserPromptSubmit":
            # New turn: reset everything and mark where this turn starts in the
            # transcript so the watcher only reads THIS turn's slice.
            path = _transcript_path(data)
            STATE["status"] = "working"
            STATE["phase"] = "thinking"
            STATE["prompt"] = data.get("prompt", "")
            STATE["answer"] = ""
            STATE["session"] = data.get("session_id", "")
            STATE["started"] = _now()
            STATE["finished"] = 0.0
            STATE["activity"] = []
            STATE["transcript"] = path
            try:
                STATE["turn_offset"] = os.path.getsize(path) if path else 0
            except OSError:
                STATE["turn_offset"] = 0

        elif event == "PreToolUse":
            label, detail = _friendly(data.get("tool_name", ""), data.get("tool_input"))
            STATE["status"] = "working"
            STATE["phase"] = "tools"
            STATE["activity"].append({"label": label, "detail": detail, "t": _now()})
            STATE["activity"] = STATE["activity"][-40:]
            # if we didn't catch the prompt start, still try to locate transcript
            if not STATE["transcript"]:
                STATE["transcript"] = _transcript_path(data)

        elif event in ("Stop", "Notification"):
            matcher = data.get("matcher", "")
            if event == "Stop" or matcher in ("idle_prompt", ""):
                if STATE["status"] == "working":
                    STATE["status"] = "done"
                    STATE["phase"] = "done"
                    STATE["finished"] = _now()


# ---------------------------------------------------------------------------
# The transcript watcher: a background loop that reads the current turn's slice
# of the transcript and pulls out the phase + the answer text as it's written.
# Text lands in chunks (whole paragraphs), not letters - the BOARD animates the
# reveal so it still feels like watching it type.
# ---------------------------------------------------------------------------
def _read_turn():
    with LOCK:
        path = STATE["transcript"]
        offset = STATE["turn_offset"]
        working = STATE["status"] == "working"

    if not working or not path or not os.path.exists(path):
        return

    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            raw = fh.read()
    except OSError:
        return

    text = raw.decode("utf-8", "ignore")
    lines = text.split("\n")
    if not text.endswith("\n"):
        lines = lines[:-1]   # drop a half-written last line

    answer_parts = []
    last_kind = None
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if o.get("type") != "assistant":
            continue
        for b in o.get("message", {}).get("content", []):
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                t = b.get("text", "")
                if t.strip():
                    answer_parts.append(t)
                    last_kind = "writing"
            elif bt == "thinking":
                last_kind = "thinking"
            elif bt == "tool_use":
                last_kind = "tools"

    with LOCK:
        if STATE["status"] == "working":
            STATE["answer"] = "\n\n".join(answer_parts)
            if last_kind:
                STATE["phase"] = last_kind


def _watcher_loop():
    while True:
        try:
            _read_turn()
        except Exception:
            pass
        time.sleep(0.4)


# ---------------------------------------------------------------------------
# Parking lot storage.
# ---------------------------------------------------------------------------
def _load_notes():
    if os.path.exists(NOTES_FILE):
        with open(NOTES_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line:
                    NOTES.append(line)


def _save_note(text):
    NOTES.append(text)
    with open(NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")


# ---------------------------------------------------------------------------
# Token analytics: where are your tokens (and dollars) actually going?
#
# Claude Code records the real token usage of every message in the same
# transcript files. We mine them and add up the numbers per project, per
# session, and per query. The big insight this surfaces: most "input" tokens
# are CACHE READS, which cost 1/10th of fresh input - so raw token counts lie
# about cost. We price each message exactly using the recorded cache split.
# ---------------------------------------------------------------------------

# Price per 1,000,000 tokens: (fresh input, output). Cache read = input x 0.1,
# cache write = input x 1.25 (5-min) or x 2.0 (1-hour).
PRICING = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}
DEFAULT_RATE = (5.0, 25.0)  # assume Opus pricing if the model is unknown

_SCAN_CACHE = {}  # path -> ((mtime, size), parsed result) so we only re-read changed files


def _msg_cost(model, u):
    inp, out = PRICING.get(model, DEFAULT_RATE)
    inp /= 1_000_000.0
    out /= 1_000_000.0
    cc = u.get("cache_creation") or {}
    if cc:
        e5 = cc.get("ephemeral_5m_input_tokens", 0)
        e1 = cc.get("ephemeral_1h_input_tokens", 0)
    else:
        e5 = u.get("cache_creation_input_tokens", 0)
        e1 = 0
    return (u.get("input_tokens", 0) * inp
            + u.get("output_tokens", 0) * out
            + u.get("cache_read_input_tokens", 0) * inp * 0.1
            + e5 * inp * 1.25
            + e1 * inp * 2.0)


def _blank():
    return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}


def _add(bucket, model, u):
    cc = u.get("cache_creation") or {}
    if cc:
        cw = cc.get("ephemeral_5m_input_tokens", 0) + cc.get("ephemeral_1h_input_tokens", 0)
    else:
        cw = u.get("cache_creation_input_tokens", 0)
    bucket["input"] += u.get("input_tokens", 0)
    bucket["output"] += u.get("output_tokens", 0)
    bucket["cache_read"] += u.get("cache_read_input_tokens", 0)
    bucket["cache_write"] += cw
    bucket["cost"] += _msg_cost(model, u)


def _clean_prompt(s):
    # strip command/caveat wrapper tags so query labels read cleanly
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _scan_one(path):
    """Read one transcript file and total its tokens by turn."""
    totals = _blank()
    by_model = {}
    turns = []
    cur = None
    started = None
    title = None
    first_real = None  # first genuine user prompt, for a title fallback
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None

    for line in raw.decode("utf-8", "ignore").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = o.get("type")

        if t == "user":
            # A real prompt starts a new "turn". A user entry that only carries
            # a tool_result is the agent continuing, not you - so skip those.
            content = o.get("message", {}).get("content")
            text = None
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        text = b.get("text")
                        break
            if text and text.strip():
                clean = _clean_prompt(text)
                cur = {"prompt": clean[:140], "totals": _blank()}
                turns.append(cur)
                if first_real is None and len(clean) > 15 and not clean.lower().startswith("caveat"):
                    first_real = clean[:90]

        elif t == "ai-title":
            at = o.get("aiTitle")
            if at:
                title = at  # last title wins (Claude Code refines it over time)

        elif t == "assistant":
            msg = o.get("message", {})
            u = msg.get("usage")
            if not u:
                continue
            model = msg.get("model", "unknown")
            if started is None:
                started = o.get("timestamp")
            _add(totals, model, u)
            _add(by_model.setdefault(model, _blank()), model, u)
            if cur is not None:
                _add(cur["totals"], model, u)

    return {"session": os.path.basename(path)[:-6], "started": started, "title": title,
            "first_real": first_real, "totals": totals, "by_model": by_model, "turns": turns}


def _scan_cached(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    hit = _SCAN_CACHE.get(path)
    if hit and hit[0] == key:
        return hit[1]
    res = _scan_one(path)
    _SCAN_CACHE[path] = (key, res)
    return res


def _pretty_path(munged):
    # "C--Users-Parshiv-foo" -> "C:\Users\Parshiv\foo"
    p = re.sub(r"^([A-Za-z])--", r"\1:\\", munged)
    return p.replace("-", "\\")


def _scan_tokens():
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    grand = _blank()
    grand_models = {}
    sessions = []

    if os.path.isdir(base):
        for proj in os.listdir(base):
            pdir = os.path.join(base, proj)
            if not os.path.isdir(pdir):
                continue
            # Only top-level session files - subagent logs live in subfolders.
            for f in os.listdir(pdir):
                if not f.endswith(".jsonl"):
                    continue
                res = _scan_cached(os.path.join(pdir, f))
                if not res or res["totals"]["output"] == 0:
                    continue
                for k in grand:
                    grand[k] += res["totals"][k]
                for m, b in res["by_model"].items():
                    gm = grand_models.setdefault(m, _blank())
                    for k in gm:
                        gm[k] += b[k]
                # a readable label: title -> first prompt -> short id
                label = res.get("title") or res.get("first_real")
                if not label and res["turns"]:
                    label = res["turns"][0]["prompt"]
                if not label:
                    label = "Session " + res["session"][:8]
                models = sorted(res["by_model"].keys())
                sessions.append({
                    "id": res["session"],
                    "title": label,
                    "project": _pretty_path(proj),
                    "started": res["started"],
                    "models": models,
                    "totals": res["totals"],
                    "turns": sorted(res["turns"], key=lambda x: x["totals"]["cost"], reverse=True)[:60],
                })

    sessions.sort(key=lambda s: s["totals"]["cost"], reverse=True)
    by_model = [dict(model=m, **b) for m, b in grand_models.items()]
    by_model.sort(key=lambda x: x["cost"], reverse=True)
    with LOCK:
        current = STATE.get("session", "")
    return {"totals": grand, "by_model": by_model, "sessions": sessions, "current_session": current}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/state":
            with LOCK:
                out = dict(STATE)
            out["now"] = _now()
            out["notes"] = NOTES[-50:]
            self._send_json(out)

        elif self.path == "/tokens" or self.path.startswith("/tokens.html"):
            with open(os.path.join(HERE, "tokens.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/api/tokens":
            self._send_json(_scan_tokens())

        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        data = self._read_body()
        if self.path == "/event":
            _handle_event(data)
            self._send_json({"ok": True})
        elif self.path == "/park":
            text = (data.get("text") or "").strip()
            if text:
                _save_note(text)
            self._send_json({"ok": True, "count": len(NOTES)})
        else:
            self._send_json({"error": "not found"}, 404)


def main():
    _load_notes()
    threading.Thread(target=_watcher_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("FocusBoard running.")
    print("Open this in a browser tab next to your terminal:")
    print("    http://localhost:%d" % PORT)
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
