#!/usr/bin/env python3
"""Cross-review loop between Claude Code and Gemini (Antigravity CLI `agy`).

  review  one review round: Gemini reads the work read-only and returns a JSON verdict
  build   Gemini as author: Gemini edits files from a brief (accept-edits mode)
  note    log Claude's own review JSON when Gemini is the author
  status  show loop state and token use
  watch   open the live dashboard (watch.py) in a Terminal window

State lives in <dir>/.crossreview/: REVIEW_LOG.md (readable transcript), live.log
(streamed Gemini output), state.json + activity.json (read by the dashboard),
per-round prompts/results and snapshots.
"""

import argparse
import datetime
import difflib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "verdict.schema.json"
WATCH = HERE / "watch.py"
DEFAULT_MODEL = "gemini-4-argon-high"
DEFAULT_MAX_ROUNDS = 4
# User-editable defaults. The first existing file wins per key; a command-line
# flag (e.g. --max-rounds 6) still overrides for that one run.
CONFIG_PATHS = [Path.home() / ".claude" / "crossreview.json"]


def load_config():
    """Persistent defaults from ~/.claude/crossreview.json (keys: max_rounds, model, watch)."""
    cfg = {}
    for p in CONFIG_PATHS:
        try:
            if p.exists():
                cfg.update(json.loads(p.read_text()))
        except Exception:
            pass  # a broken config must never stop a review
    return cfg
SD_NAME = ".crossreview"
SKIP_DIRS = {".git", SD_NAME, "node_modules", ".venv", "venv", "__pycache__",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", "DerivedData", ".gradle"}
MAX_FILES = 5000
TEXT_MAX = 1_000_000    # bytes; larger files are tracked by fingerprint only
HASH_MAX = 20_000_000   # bytes; larger files are fingerprinted by size + mtime
DIFF_MAX = 60_000       # chars of diff passed to the reviewer
BLOCKING = ("blocker", "major")
LINK = re.compile(r"\[([^\]]*)\]\(file://[^)]*\)")
# author replies such as "R1-2 FIXED: ..." or "**R1-2**: REBUTTED - ..."
REPLY = re.compile(r"\b(R\d+-\d+)\b\W{0,12}(FIXED|REBUTTED|DEFERRED)\b\W{0,6}([^\n]*)", re.I)

CHARTER = """You are the independent REVIEWER in a critical-review loop. Another AI (Claude) is the AUTHOR of the work in this workspace. You review, the author fixes or rebuts, and the loop repeats until you approve or the round limit is hit. Be precise rather than exhaustive.

Rules
1. Read-only. Do not create, edit or delete files in the workspace and do not offer to apply fixes. Read whatever you need.
2. Evidence or it is not a finding. Every finding needs concrete evidence: file:line, quoted code or text, a counterexample input, or a calculation. Unverifiable hunches are not findings.
3. Calibrated. If the work is correct, APPROVE. Do not invent issues to look thorough, and do not approve to be agreeable.
4. Severity:
   - blocker: wrong result or conclusion, crash, data loss, security hole, or an explicit requirement violated.
   - major: likely bug or failing edge case, missing requirement, misleading output or claim, a figure or statistic that does not support the text.
   - minor: style, naming, clarity, nice-to-have. Never blocks approval.
5. IDs: new findings in round N get ids RN-1, RN-2, ... An earlier finding that is still unresolved is re-listed with its ORIGINAL id. Ids you now consider settled (fixed, or the author's rebuttal convinced you) go in `resolved`.
6. Rebuttals: when the author rebuts a finding, either accept it (id into `resolved`) or keep it with NEW evidence. Do not repeat the same argument.
7. Ignore the `.crossreview/` folder; it is loop bookkeeping.
8. Verdict: APPROVE if and only if no blocker or major finding remains."""

REMINDER = """This is review round {r} of at most {m}. Read the files you need from disk; they may have changed since you last looked. Apply the reviewer rules. New finding ids: R{r}-1, R{r}-2, ...; still-open earlier findings keep their original id; settled ids go in `resolved`. Reply with a short prose review, then the JSON object."""

AUTHOR_CHARTER = """You are the AUTHOR in a two-AI review loop. Another AI (Claude) will critically review your work when you finish, and you may get its findings in later rounds.
- Do the task by editing files in this workspace. Stay within the task and do not touch `.crossreview/` (loop bookkeeping).
- Verify your work where you can and say exactly what you verified. Do not claim checks you did not run.
- Finish with a short report: files changed, how you verified, known limitations or open questions."""


def ts():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def parse_time(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def clean(text):
    return LINK.sub(r"\1", text)


# ---------- token accounting ----------

def claude_transcript():
    """This Claude Code session's transcript, or None when not run from Claude Code."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    hits = sorted(Path.home().glob(f".claude/projects/*/{sid}.jsonl"), key=lambda p: p.stat().st_mtime) if sid else []
    return hits[-1] if hits else None


def claude_usage(since):
    """Sum Claude's usage in this session's transcript after `since` (ISO time).

    Without `since`, counts from the user's last prompt. None when not run from Claude Code.
    """
    path = claude_transcript()
    if not path:
        return None
    msgs, last_prompt = {}, None
    with open(path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            m, t = e.get("message"), e.get("timestamp")
            if not isinstance(m, dict) or not t:
                continue
            if e.get("type") == "assistant" and m.get("usage"):
                msgs[m.get("id")] = (t, m["usage"])  # streamed chunks repeat the id; keep one
            elif e.get("type") == "user":
                content = m.get("content")
                if isinstance(content, str) or any(c.get("type") != "tool_result" for c in content or []):
                    last_prompt = t
    start = parse_time(since or last_prompt or "1970-01-01T00:00:00+00:00")
    tot = {"calls": 0, "in": 0, "cached": 0, "out": 0, "thinking": 0}
    for t, u in msgs.values():
        if parse_time(t) <= start:
            continue
        tot["calls"] += 1
        tot["in"] += u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
        tot["cached"] += u.get("cache_read_input_tokens", 0)
        tot["out"] += u.get("output_tokens", 0)
        tot["thinking"] += (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)
    return tot


def gemini_usage(res):
    u = res.get("usage") or {}
    return {"in": u.get("input_tokens", 0), "cached": u.get("cache_read_tokens", 0),
            "out": u.get("output_tokens", 0), "thinking": u.get("thinking_tokens", 0)}


def fmt_tokens(u):
    if not u:
        return "n/a"
    if u.get("calls") == 0 and not u.get("out"):
        return "0 (no new turns)"
    k = lambda n: f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k"
    s = f"in {k(u['in'])} (+{k(u['cached'])} cached) · out {k(u['out'])} (incl. {k(u['thinking'])} thinking)"
    n = u.get("calls")
    return s + (f" · {n} call{'s' if n != 1 else ''}" if n else "")


def record_tokens(st, rnd, agent, activity, usage):
    """Store one agent's usage for a round; returns the line shown in output and log."""
    st.setdefault("tokens", {}).setdefault(str(rnd), {})[agent] = {"activity": activity, "usage": usage}
    return f"TOKENS {agent} ({activity}): {fmt_tokens(usage)}"


def token_totals(st):
    tot = {}
    for per_round in st.get("tokens", {}).values():
        for agent, rec in per_round.items():
            if rec["usage"]:
                acc = tot.setdefault(agent.split()[0], {})  # "Claude review" counts as Claude
                for key, val in rec["usage"].items():
                    acc[key] = acc.get(key, 0) + val
    return tot


# ---------- state ----------

def state_dir(d):
    return d / SD_NAME


def load_state(d):
    p = state_dir(d) / "state.json"
    return json.loads(p.read_text()) if p.exists() else None


def write_json(p, obj):
    # atomic, so the dashboard never reads a half-written file
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, p)


def save_state(d, st):
    write_json(state_dir(d) / "state.json", st)


def set_activity(d, who, what, rnd, running=False, done=False):
    """What the dashboard shows under NOW."""
    write_json(state_dir(d) / "activity.json", {"who": who, "what": what, "round": rnd, "running": running,
                                                "done": done, "since": utcnow()})


def log(d, text):
    with open(state_dir(d) / "REVIEW_LOG.md", "a") as f:
        f.write(text.rstrip() + "\n\n")


def archive(d):
    sd = state_dir(d)
    if not (sd / "state.json").exists():
        return
    dest = sd / "archive" / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True)
    for p in sd.iterdir():
        if p.name != "archive":
            shutil.move(str(p), dest / p.name)


def git_exclude(d):
    # keep loop bookkeeping out of `git status` without touching .gitignore
    r = subprocess.run(["git", "-C", str(d), "rev-parse", "--absolute-git-dir"],
                       capture_output=True, text=True)
    if r.returncode:
        return
    ex = Path(r.stdout.strip()) / "info" / "exclude"
    ex.parent.mkdir(exist_ok=True)
    cur = ex.read_text() if ex.exists() else ""
    if f"{SD_NAME}/" not in cur.splitlines():
        with open(ex, "a") as f:
            f.write(("\n" if cur and not cur.endswith("\n") else "") + f"{SD_NAME}/\n")


def init_state(d, args, role):
    if args.new:
        archive(d)
    st = load_state(d)
    if not st:
        state_dir(d).mkdir(exist_ok=True)
        git_exclude(d)
        cfg = load_config()
        st = {"role": role, "model": cfg.get("model", DEFAULT_MODEL),
              "max_rounds": int(cfg.get("max_rounds", DEFAULT_MAX_ROUNDS)),
              "scope": ["."], "round": 0, "conversation": None, "author_conversation": None,
              "history": {}, "ledger": {}, "rounds": [], "watch_opened": False, "started": ts()}
    for key, val in (("model", args.model), ("max_rounds", args.max_rounds), ("scope", args.scope)):
        if val:
            st[key] = val
    st["claude_transcript"] = str(claude_transcript() or st.get("claude_transcript", ""))
    if st["round"] == 0:
        gem = f"Gemini via Antigravity (`{st['model']}`)"
        author, reviewer = ("Claude Code", gem + ", read-only") if role == "claude-author" else (gem, "Claude Code")
        (state_dir(d) / "REVIEW_LOG.md").write_text(
            f"# Cross-review log\n\n- Folder: `{d}`\n- Scope: {', '.join(st['scope'])}\n"
            f"- Author: {author}\n- Reviewer: {reviewer}\n- Started: {st['started']}\n"
            f"- Max rounds: {st['max_rounds']}\n\n")
        (state_dir(d) / "live.log").touch()
    return st


# ---------- snapshots and diffs ----------

def resolve_scope(d, scope):
    out = []
    for s in scope:
        p = Path(s).expanduser()
        out.append((p if p.is_absolute() else d / p).resolve())
    return out


def disp(d, p):
    return str(p.relative_to(d)) if p.is_relative_to(d) else str(p)


def iter_files(d, scope):
    seen = set()
    for root in resolve_scope(d, scope):
        if root.is_file():
            cands = [root]
        else:
            cands = []
            for r, dirs, files in os.walk(root):
                dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS)
                cands += [Path(r) / f for f in sorted(files) if f != ".DS_Store"]
        for p in cands:
            if p in seen:
                continue
            if len(seen) >= MAX_FILES:
                return
            seen.add(p)
            yield p


def snapshot(d, scope, dest=None):
    """Fingerprint every file in scope; with dest, also copy text files there for diffing."""
    man = {}
    for p in iter_files(d, scope):
        try:
            info = p.stat()
            data = p.read_bytes() if info.st_size <= HASH_MAX else None
        except OSError:
            continue
        key = disp(d, p)
        fp = hashlib.sha256(data).hexdigest() if data is not None else f"size{info.st_size}-mtime{info.st_mtime_ns}"
        text = None
        if data is not None and info.st_size <= TEXT_MAX and b"\0" not in data[:8192]:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                pass
        man[key] = {"fp": fp, "text": text is not None}
        if dest and text is not None:
            out = dest / "files" / key.lstrip("/")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text)
    if dest:
        (dest / "manifest.json").write_text(json.dumps(man))
    return man


def _lines(snapdir, key, entry):
    if not entry:
        return []
    lines = (snapdir / "files" / key.lstrip("/")).read_text().splitlines(True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    return lines


def diff_snapshots(old_dir, old, new_dir, new):
    """Return (list of 'changed: path' lines, full unified diff of text files)."""
    changes, chunks = [], []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a and b and a["fp"] == b["fp"]:
            continue
        tag = "added" if a is None else "removed" if b is None else "changed"
        if (a is None or a["text"]) and (b is None or b["text"]):
            changes.append(f"{tag}: {key}")
            chunks.append("".join(difflib.unified_diff(
                _lines(old_dir, key, a), _lines(new_dir, key, b), f"a/{key}", f"b/{key}")))
        else:
            changes.append(f"{tag} (binary or large, no diff): {key}")
    return changes, "".join(chunks)


def change_block(changes, diff):
    if not changes:
        return "(no file changes)"
    if len(diff) > DIFF_MAX:
        diff = diff[:DIFF_MAX] + f"\n[diff truncated, {len(diff) - DIFF_MAX} more chars: read the files directly]\n"
    return "\n".join(f"- {c}" for c in changes) + (f"\n\n```diff\n{diff}```" if diff else "")


def file_list(keys, limit=200):
    keys = sorted(keys)
    more = f"\n- ... and {len(keys) - limit} more" if len(keys) > limit else ""
    return "\n".join(f"- {k}" for k in keys[:limit]) + more


# ---------- agy ----------

class Live:
    """Line-buffered, human-readable transcript of the agy stream (shown in the dashboard)."""

    def __init__(self, path, d):
        self.f = open(path, "a", buffering=1)
        self.root = str(d) + "/"
        self.buf = ""

    def _write(self, s):
        if s.lstrip().startswith("{") and '"verdict"' in s:
            _, v = split_json(s)
            if v:
                s = "\n".join([f"VERDICT: {v.get('verdict')} · {v.get('summary', '')}"] +
                              [f"  [{f.get('id')}] {f.get('severity')} {where(f)}: {f.get('issue')}"
                               for f in v.get("findings") or []])
        self.f.write(clean(s).replace(self.root, "") + "\n")

    def text(self, s):
        self.buf += s
        *lines, self.buf = self.buf.split("\n")
        for line in lines:
            self._write(line)

    def flush(self):
        if self.buf:
            self._write(self.buf)
            self.buf = ""

    def line(self, s):
        self.flush()
        self._write(s)

    def step(self, su):
        if su.get("step_type") == "agent_response":
            self.text(su.get("text_delta", ""))
            if su.get("state") == "DONE":
                self.flush()
        elif su.get("step_type") == "tool" and su.get("state") == "ACTIVE":
            params = (su.get("tool_info") or {}).get("parameters") or {}
            arg = next((v for v in params.values() if isinstance(v, str)), "")
            self.line(f"  > {su.get('tool_name')} {arg[:150]}")


def agy_bin():
    found = shutil.which("agy")
    if found:
        return found
    if os.name == "nt":
        la = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))
        for cand in (Path(la) / "Antigravity/agy.exe", Path(la) / "agy/bin/agy.exe"):
            if cand.exists():
                return str(cand)
        return "agy"  # let the OS resolve it / fail with a clear message
    return str(Path.home() / ".local/bin/agy")


def run_agy(d, st, prompt, mode, conv_key, timeout, schema, label):
    """Run one agy turn, streaming into live.log. Returns the result dict, or None on failure."""
    sd = state_dir(d)
    cmd = [agy_bin(), "-p", prompt, "--mode", mode, "--model", st["model"],
           "--output-format", "stream-json", "--print-timeout", f"{timeout}s"]
    if schema:
        cmd += ["--json-schema", str(SCHEMA)]
    if st.get(conv_key):
        cmd += ["--conversation", st[conv_key]]
    for p in resolve_scope(d, st["scope"]):
        if not p.is_relative_to(d):
            cmd += ["--add-dir", str(p if p.is_dir() else p.parent)]
    live = Live(sd / "live.log", d)
    live.line(f"\n===== {label} · {ts()} · {st['model']} =====")
    result, model = None, None
    with open(sd / "agy-stderr.log", "a") as err:
        proc = subprocess.Popen(cmd, cwd=d, stdout=subprocess.PIPE, stderr=err, text=True)
        killer = threading.Timer(timeout + 120, proc.kill)
        killer.start()
        for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = ev.get("event")
            if kind == "init":
                model = (ev.get("init") or {}).get("model")
            elif kind == "step_update":
                live.step(ev["step_update"])
            elif kind == "result":
                result = ev["result"]
        proc.wait()
        killer.cancel()
    if not result or result.get("status") != "SUCCESS":
        live.line(f"===== failed (exit {proc.returncode}, status {result and result.get('status')}) =====")
        return None
    live.line(f"===== {label} finished in {result.get('duration_seconds', 0):.0f}s =====")
    if result.get("conversation_id"):
        st[conv_key] = result["conversation_id"]
    result["model_used"] = model or st["model"]
    return result


def run_with_retry(d, st, build_prompt, mode, conv_key, timeout, schema, label, prompt_path):
    """A resumed conversation can expire; retry once as a fresh conversation."""
    had = bool(st.get(conv_key))
    prompt = build_prompt(not had)
    prompt_path.write_text(prompt)
    res = run_agy(d, st, prompt, mode, conv_key, timeout, schema, label)
    if res is None and had:
        st[conv_key] = None
        prompt = build_prompt(True)
        prompt_path.write_text(prompt)
        res = run_agy(d, st, prompt, mode, conv_key, timeout, schema, label + " (fresh retry)")
    if res is None:
        set_activity(d, "Claude", f"{label} failed: see .crossreview/live.log", st["round"] + 1)
        sys.exit(f"agy failed. See {state_dir(d) / 'live.log'} and {state_dir(d) / 'agy-stderr.log'}")
    return res


def split_json(text):
    """Return (prose, obj): obj is the last JSON object in text that carries a verdict."""
    dec = json.JSONDecoder()
    for m in reversed(list(re.finditer(r"\{", text))):
        try:
            obj, end = dec.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "verdict" in obj:
            return text[:m.start()] + text[end:], obj
    return text, None


def open_watch(d):
    """Open the dashboard (watch.py) in a new terminal window. Cross-platform."""
    title = f"crossreview · {d.name}"
    # Windows: open a new console window. `start` takes the window title first.
    if os.name == "nt":
        try:
            subprocess.Popen(
                f'start "{title}" "{sys.executable}" "{WATCH}" --dir "{d}"',
                shell=True)
            return True
        except Exception:
            return False
    # Linux / other X11 desktops: try a terminal emulator.
    if sys.platform != "darwin":
        watch_cmd = [sys.executable, str(WATCH), "--dir", str(d)]
        for term, flag in (("x-terminal-emulator", "-e"), ("gnome-terminal", "--"),
                           ("konsole", "-e"), ("xterm", "-e")):
            if shutil.which(term):
                try:
                    subprocess.Popen([term, flag, *watch_cmd])
                    return True
                except Exception:
                    pass
        return False
    # macOS: a new white Terminal window.
    q = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    cmd = f"clear; {shlex.quote(sys.executable)} {shlex.quote(str(WATCH))} --dir {shlex.quote(str(d))}"
    script = f'''tell application "Terminal"
    set t to do script "{q(cmd)}"
    try
        set current settings of t to settings set "Basic"
    end try
    try
        set custom title of t to "crossreview · {q(d.name)}"
        set title displays shell path of t to false
        set title displays device name of t to false
        set title displays window size of t to false
        set title displays settings name of t to false
    end try
    try
        set number of columns of front window to 132
        set number of rows of front window to 58
    end try
end tell'''
    return subprocess.run(["osascript", "-e", script], capture_output=True).returncode == 0


# ---------- verdicts and the findings ledger ----------

def decide(st, rnd, v):
    findings = v.get("findings") or []
    blocking = [f for f in findings if f.get("severity") in BLOCKING]
    for f in blocking:
        rounds = st["history"].setdefault(f.get("id", "?"), [])
        if rnd not in rounds:
            rounds.append(rnd)
    stuck = [i for i, rs in st["history"].items() if rnd in rs and len(rs) >= 3]
    if not blocking:
        nxt = "DONE (approved)" if v.get("verdict") == "APPROVE" else "DONE (only minor findings left)"
    elif rnd >= st["max_rounds"]:
        nxt = "STOP (max rounds reached: take the open findings to the user)"
    else:
        nxt = "CONTINUE"
    return blocking, stuck, nxt


def sev_counts(findings):
    return {s: sum(f.get("severity") == s for f in findings) for s in ("blocker", "major", "minor")}


def ledger_author(st, rnd, who, text):
    """Apply the author's per-finding replies to the ledger; returns counts per reply type."""
    led, counts, seen = st.setdefault("ledger", {}), {"fixed": 0, "rebutted": 0, "deferred": 0}, set()
    for fid, word, rest in REPLY.findall(text):
        fid, word = fid.upper(), word.lower()
        e = led.get(fid)
        if not e or fid in seen:
            continue
        seen.add(fid)
        counts[word] += 1
        e["status"] = word
        ev = [rnd, who, f"{word}: {rest.strip()[:200]}"]
        if ev not in e["events"]:
            e["events"].append(ev)
    return counts


def ledger_review(st, rnd, who, v, stuck):
    """Update finding states from a verdict: new, resolved, accepted, disputed, reopened, dropped."""
    led, listed = st.setdefault("ledger", {}), set()
    for fid in v.get("resolved") or []:
        e = led.get(fid)
        if not e:
            continue
        listed.add(fid)
        rebutted = e["status"] in ("rebutted", "disputed")
        e["status"], e["closed"] = ("accepted" if rebutted else "resolved"), rnd
        e["events"].append([rnd, who, "accepted the rebuttal" if rebutted else "confirmed resolved"])
    for f in v.get("findings") or []:
        fid = f.get("id", "?")
        listed.add(fid)
        info = {"sev": f.get("severity", "?"), "where": where(f), "issue": f.get("issue", "").strip()}
        evidence = (f.get("evidence") or "").strip()[:200]
        e = led.get(fid)
        if not e:
            led[fid] = {**info, "raised": rnd, "by": who, "status": "open",
                        "events": [[rnd, who, f"raised ({info['sev']}): {info['issue'][:200]}"]]}
            continue
        prev = e["status"]
        e.update(info)
        if prev in ("rebutted", "disputed"):
            e["status"] = "disputed"
            e["events"].append([rnd, who, f"kept it: {evidence}"])
        elif prev == "fixed":
            e["status"] = "reopened"
            e["events"].append([rnd, who, f"fix not accepted: {evidence}"])
        elif prev in ("resolved", "accepted", "dropped"):
            e["status"] = "open"
            e["events"].append([rnd, who, "raised again"])
    for fid, e in led.items():
        if fid in stuck:
            e["status"] = "escalated"
        elif fid not in listed and e["status"] in ("open", "fixed", "reopened", "rebutted", "disputed"):
            # reviewer silently dropped it: a claimed fix or rebuttal counts as accepted
            e["status"] = {"fixed": "resolved", "rebutted": "accepted", "disputed": "accepted"}.get(e["status"], "dropped")
            e["closed"] = rnd
            e["events"].append([rnd, who, "no longer listed"])


def after_verdict(d, st, rnd, nxt, stuck):
    """Tell the dashboard who acts now and what comes next."""
    if nxt.startswith("DONE"):
        set_activity(d, "Claude", f"loop finished in round {rnd}: {nxt[6:-1]}", rnd, done=True)
        st["next_step"] = {"who": "Claude", "what": "final summary to you"}
        return
    if nxt.startswith("STOP"):
        set_activity(d, "You", f"round limit reached after round {rnd}", rnd, done=True)
        st["next_step"] = {"who": "You", "what": "decide on the open findings"}
        return
    if st["role"] == "claude-author":
        set_activity(d, "Claude", "checking the findings, then fixing or rebutting", rnd)
        nxt_step = {"who": "Gemini", "what": f"re-review in round {rnd + 1}"}
    else:
        set_activity(d, "Claude", f"starting Gemini's round {rnd + 1}", rnd)
        nxt_step = {"who": "Gemini", "what": f"fix or rebut Claude's findings in round {rnd + 1}"}
    st["next_step"] = {"who": "You", "what": f"decide on {', '.join(stuck)} (blocking for 3 rounds)"} if stuck else nxt_step


def where(f):
    return f.get("file", "?") + (f":{f['line']}" if f.get("line") else "")


def render_findings(findings):
    out = []
    for f in findings:
        out.append(f"- **{f.get('id', '?')}** ({f.get('severity')}) `{where(f)}`: {f.get('issue', '').strip()}")
        for label, key in (("Evidence", "evidence"), ("Suggestion", "suggestion")):
            if f.get(key):
                out.append(f"  - {label}: " + f[key].strip().replace("\n", "\n    "))
    return "\n".join(out) or "- none"


def render_review(who, rnd, v, prose, blocking, stuck, nxt, warn, secs=None):
    took = f" · {secs:.0f}s" if secs else ""
    parts = [f"## Round {rnd} · Review by {who} · {'CHANGES' if blocking else 'APPROVE'}{took}",
             f"**Summary:** {v.get('summary', '').strip()}",
             render_findings(v.get("findings") or [])]
    if v.get("resolved"):
        parts.append("**Resolved:** " + ", ".join(v["resolved"]))
    if prose.strip():
        parts.append("<details><summary>Full reviewer text</summary>\n\n" + clean(prose).strip() + "\n\n</details>")
    parts += warn
    if stuck:
        parts.append("**Escalate to user** (blocking for 3 rounds): " + ", ".join(stuck))
    parts.append(f"**Next:** {nxt}")
    return "\n\n".join(parts)


def summary(d, rnd, who, v, blocking, stuck, nxt, warn, secs=None):
    fs = v.get("findings") or []
    n = sev_counts(fs)
    took = f" · {secs:.0f}s" if secs else ""
    out = [f"ROUND {rnd} · reviewer: {who}{took}",
           f"VERDICT: {'CHANGES' if blocking else 'APPROVE'} "
           f"({n['blocker']} blocker, {n['major']} major, {n['minor']} minor)",
           f"SUMMARY: {v.get('summary', '').strip()}"]
    out += [f"  [{f.get('id', '?')}] {f.get('severity', '?'):7} {where(f)}: {f.get('issue', '').strip()[:300]}" for f in fs]
    if v.get("resolved"):
        out.append("RESOLVED: " + ", ".join(v["resolved"]))
    if stuck:
        out.append("ESCALATE (blocking for 3 rounds, ask the user): " + ", ".join(stuck))
    out += warn
    out += [f"NEXT: {nxt}", f"LOG: {state_dir(d) / 'REVIEW_LOG.md'}"]
    return "\n".join(out)


def ensure_watch(d, st, args):
    want = load_config().get("watch", True)  # set "watch": false in the config to default off
    if not st["watch_opened"] and not args.no_watch and want:
        st["watch_opened"] = open_watch(d)


# ---------- commands ----------

def resolve_dir(s):
    d = Path(s).expanduser().resolve()
    if not d.is_dir():
        sys.exit(f"not a directory: {d}")
    return d


def cmd_review(args):
    d = resolve_dir(args.dir)
    st = init_state(d, args, "claude-author")
    brief = Path(args.brief).read_text() if args.brief else ""
    rnd = st["round"] + 1
    if rnd == 1 and not brief.strip():
        sys.exit("round 1 needs --brief")
    if rnd == 1:
        st["brief"] = brief
    sd = state_dir(d)
    tok_claude = record_tokens(st, rnd, "Claude", "build + brief" if rnd == 1 else "fixes + response",
                               claude_usage(st.get("last_call_end")))
    snapdir = sd / "snapshots" / f"round-{rnd:02d}"
    shutil.rmtree(snapdir, ignore_errors=True)
    snapdir.mkdir(parents=True)
    before = snapshot(d, st["scope"], snapdir)
    changes, diff = [], ""
    if rnd > 1:
        prev = sd / "snapshots" / f"round-{rnd - 1:02d}"
        changes, diff = diff_snapshots(prev, json.loads((prev / "manifest.json").read_text()), snapdir, before)

    # show the author's replies and Gemini's turn on the dashboard before the review starts
    st["rounds"] = [r for r in st.get("rounds", []) if r["round"] < rnd]
    replies = ledger_author(st, rnd, "Claude", brief) if rnd > 1 else None
    st["rounds"].append({"round": rnd, "who": "Claude", "kind": "build" if rnd == 1 else "respond",
                         "replies": replies, "changed": len(changes), "tok": "Claude"})
    st["next_step"] = {"who": "Claude", "what": "act on Gemini's verdict"}
    set_activity(d, "Gemini", f"reviewing round {rnd}", rnd, running=True)
    ensure_watch(d, st, args)
    save_state(d, st)

    def build_prompt(fresh):
        parts = [CHARTER] if fresh else []
        if rnd == 1 or fresh:
            parts.append("## Files in scope\n" + file_list(before))
        if rnd == 1:
            parts.append("## Author's brief\n" + brief.strip())
        else:
            if fresh:
                parts.append("## Original brief\n" + st.get("brief", "").strip())
                parts.append(f"## Your findings from round {rnd - 1}\n" + render_findings(st.get("last", {}).get("findings", [])))
            parts.append(f"## Round {rnd}: author's response to your findings\n" + (brief.strip() or "(no written response)"))
            parts.append(f"## Changes since round {rnd - 1}\n" + change_block(changes, diff))
        parts.append(REMINDER.format(r=rnd, m=st["max_rounds"]))
        return "\n\n".join(parts)

    res = run_with_retry(d, st, build_prompt, "plan", "conversation", args.timeout, True,
                         f"Round {rnd} review", sd / f"round-{rnd:02d}.prompt.md")
    prose, parsed = split_json(res.get("response", ""))
    v = res.get("structured_output") or parsed
    if not isinstance(v, dict) or "verdict" not in v:
        set_activity(d, "Claude", "Gemini returned no parsable verdict: see live.log", rnd)
        sys.exit(f"reviewer returned no parsable verdict; see {sd / 'live.log'}")
    (sd / f"round-{rnd:02d}.result.json").write_text(json.dumps(res, indent=2))

    after = snapshot(d, st["scope"])
    touched = sorted(k for k in set(before) | set(after) if before.get(k, {}).get("fp") != after.get(k, {}).get("fp"))
    warn = []
    if touched:
        warn.append("WARNING: files changed during the read-only review: " + ", ".join(touched[:20]))
    if not res["model_used"].startswith("gemini"):
        warn.append(f"WARNING: reviewer model was {res['model_used']}, not Gemini")
    blocking, stuck, nxt = decide(st, rnd, v)
    if v.get("verdict") == "APPROVE" and blocking:
        warn.append("NOTE: reviewer said APPROVE but listed blocker/major findings; treated as CHANGES")
    ledger_review(st, rnd, "Gemini", v, stuck)
    secs = res.get("duration_seconds")
    warn += [tok_claude, record_tokens(st, rnd, "Gemini", "review", gemini_usage(res))]
    st["rounds"].append({"round": rnd, "who": "Gemini", "kind": "review", "verdict": "CHANGES" if blocking else "APPROVE",
                         "counts": sev_counts(v.get("findings") or []), "secs": secs, "tok": "Gemini"})

    head = f"## Round {rnd} · {'Brief' if rnd == 1 else 'Response'} from Claude (author)\n\n{brief.strip() or '(none)'}"
    if rnd > 1:
        head += "\n\n**Changed since last round:**\n" + ("\n".join(f"- {c}" for c in changes) or "- nothing")
    log(d, head)
    who = f"Gemini ({res['model_used']})"
    log(d, render_review(who, rnd, v, prose, blocking, stuck, nxt, warn, secs))
    st["round"] = rnd
    st["last"] = {"round": rnd, "verdict": "CHANGES" if blocking else "APPROVE", "findings": v.get("findings") or [], "next": nxt}
    st["last_call_end"] = utcnow()
    after_verdict(d, st, rnd, nxt, stuck)
    save_state(d, st)
    print(summary(d, rnd, who, v, blocking, stuck, nxt, warn, secs))


def cmd_build(args):
    d = resolve_dir(args.dir)
    st = init_state(d, args, "gemini-author")
    rnd = st["round"] + 1
    brief = Path(args.brief).read_text() if args.brief else ""
    if rnd == 1 and not brief.strip():
        sys.exit("round 1 needs --brief")
    if rnd == 1:
        st["brief"] = brief
    note = st.get("last_note")
    if rnd > 1 and note:
        brief = (f"Claude reviewed round {rnd - 1} and found:\n\n{render_findings(note.get('findings', []))}\n\n"
                 "For each blocker/major finding: fix it, or rebut it with concrete evidence. Fix minor ones if cheap. "
                 "Report per id: FIXED / REBUTTED / DEFERRED, one line each.\n\n" + brief).strip()
    sd = state_dir(d)
    tok_claude = record_tokens(st, rnd, "Claude", "brief", claude_usage(st.get("last_call_end")))
    pre = sd / "snapshots" / "pre"
    shutil.rmtree(pre, ignore_errors=True)
    pre.mkdir(parents=True)
    before = snapshot(d, st["scope"], pre)
    st["rounds"] = [r for r in st.get("rounds", []) if r["round"] < rnd]
    st["next_step"] = {"who": "Claude", "what": f"review Gemini's round {rnd} changes"}
    set_activity(d, "Gemini", f"building round {rnd}" if rnd == 1 else f"fixing or rebutting Claude's findings (round {rnd})",
                 rnd, running=True)
    ensure_watch(d, st, args)
    save_state(d, st)

    def build_prompt(fresh):
        parts = [AUTHOR_CHARTER] if fresh else []
        if fresh and rnd > 1:
            parts.append("## Original task\n" + st.get("brief", "").strip())
        parts.append(f"## Round {rnd} task\n" + brief.strip())
        return "\n\n".join(parts)

    res = run_with_retry(d, st, build_prompt, "accept-edits", "author_conversation", args.timeout, False,
                         f"Round {rnd} build", sd / f"round-{rnd:02d}.prompt.md")
    post = sd / "snapshots" / f"round-{rnd:02d}"
    shutil.rmtree(post, ignore_errors=True)
    post.mkdir(parents=True)
    after = snapshot(d, st["scope"], post)
    changes, diff = diff_snapshots(pre, before, post, after)
    shutil.rmtree(pre)
    diff_path = sd / f"round-{rnd:02d}.diff"
    diff_path.write_text(diff)
    (sd / f"round-{rnd:02d}.result.json").write_text(json.dumps(res, indent=2))

    report = clean(res.get("response", "")).strip()
    changed = "\n".join(f"- {c}" for c in changes) or "- nothing"
    tok = "\n".join([tok_claude, record_tokens(st, rnd, "Gemini", "build", gemini_usage(res))])
    replies = ledger_author(st, rnd, "Gemini", report) if rnd > 1 else None
    st["rounds"].append({"round": rnd, "who": "Gemini", "kind": "build" if rnd == 1 else "respond", "replies": replies,
                         "changed": len(changes), "secs": res.get("duration_seconds"), "tok": "Gemini"})
    log(d, f"## Round {rnd} · Task for Gemini (author)\n\n{brief.strip()}")
    log(d, f"## Round {rnd} · Gemini's report ({res['model_used']})\n\n{report}\n\n**Changed files:**\n{changed}\n\n{tok}")
    st["round"] = rnd
    st["last_call_end"] = utcnow()
    st["next_step"] = {"who": "Gemini", "what": f"fix or rebut Claude's findings in round {rnd + 1}, if any"}
    set_activity(d, "Claude", f"reviewing Gemini's round {rnd} changes", rnd)
    save_state(d, st)
    warn = "" if res["model_used"].startswith("gemini") else f"\nWARNING: author model was {res['model_used']}, not Gemini"
    print(f"ROUND {rnd} · Gemini ({res['model_used']}) built · {res.get('duration_seconds', 0):.0f}s{warn}\n"
          f"CHANGED:\n{changed}\nDIFF: {diff_path}\n{tok}\nREPORT:\n{report[:4000]}\n"
          f"NEXT: review the diff and files, write review JSON (verdict.schema.json), then run `note`\n"
          f"LOG: {sd / 'REVIEW_LOG.md'}")


def cmd_note(args):
    d = resolve_dir(args.dir)
    st = load_state(d)
    if not st or st["round"] == 0:
        sys.exit("no build round yet; run `build` first")
    v = json.loads(Path(args.file).read_text())
    if v.get("verdict") not in ("APPROVE", "CHANGES") or not isinstance(v.get("findings"), list):
        sys.exit("review JSON needs verdict APPROVE|CHANGES and a findings list")
    bad = [f for f in v["findings"] if f.get("severity") not in ("blocker", "major", "minor")]
    if bad:
        sys.exit(f"invalid severity in: {bad}")
    rnd = st["round"]
    blocking, stuck, nxt = decide(st, rnd, v)
    ledger_review(st, rnd, "Claude", v, stuck)
    # Claude's brief tokens were already stored for this round; the review gets its own key
    tok = [record_tokens(st, rnd, "Claude review", "review", claude_usage(st.get("last_call_end")))]
    st["rounds"] = [r for r in st.get("rounds", []) if not (r["round"] == rnd and r["kind"] == "review")]
    st["rounds"].append({"round": rnd, "who": "Claude", "kind": "review", "verdict": "CHANGES" if blocking else "APPROVE",
                         "counts": sev_counts(v["findings"]), "tok": "Claude review"})
    log(d, render_review("Claude", rnd, v, "", blocking, stuck, nxt, tok))
    st["last_note"] = v
    st["last"] = {"round": rnd, "verdict": "CHANGES" if blocking else "APPROVE", "findings": v["findings"], "next": nxt}
    st["last_call_end"] = utcnow()
    after_verdict(d, st, rnd, nxt, stuck)
    save_state(d, st)
    print(summary(d, rnd, "Claude", v, blocking, stuck, nxt, tok))


def cmd_status(args):
    d = resolve_dir(args.dir)
    st = load_state(d)
    if not st:
        sys.exit(f"no cross-review loop in {d}")
    last = st.get("last") or {}
    print(f"role: {st['role']} · model: {st['model']} · round {st['round']}/{st['max_rounds']}\n"
          f"scope: {', '.join(st['scope'])}\nlast verdict: {last.get('verdict', '-')} · next: {last.get('next', '-')}\n"
          f"log: {state_dir(d) / 'REVIEW_LOG.md'}")
    for rnd, per_round in sorted(st.get("tokens", {}).items(), key=lambda kv: int(kv[0])):
        for agent, rec in per_round.items():
            print(f"round {rnd} · {agent} ({rec['activity']}): {fmt_tokens(rec['usage'])}")
    for agent, u in token_totals(st).items():
        print(f"TOTAL {agent}: {fmt_tokens(u)}")


def cmd_watch(args):
    d = resolve_dir(args.dir)
    if not (state_dir(d) / "state.json").exists():
        sys.exit(f"no cross-review loop in {d}")
    if not open_watch(d):
        pyexe = "python" if os.name == "nt" else "python3"
        print(f"could not open a dashboard window; run: {pyexe} {shlex.quote(str(WATCH))} --dir {shlex.quote(str(d))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("review", "build", "note", "status", "watch"):
        p = sub.add_parser(name)
        p.add_argument("--dir", default=".", help="project folder (holds .crossreview/)")
        if name in ("review", "build"):
            p.add_argument("--brief", help="markdown file: round-1 brief, or the response/extra task for later rounds")
            p.add_argument("--scope", nargs="+", help="files/dirs under review (default: whole folder)")
            p.add_argument("--model", help=f"agy model id (default {DEFAULT_MODEL})")
            p.add_argument("--max-rounds", type=int)
            p.add_argument("--timeout", type=int, default=540, help="seconds per agy turn")
            p.add_argument("--no-watch", action="store_true", help="don't open the live dashboard window")
            p.add_argument("--new", action="store_true", help="archive any existing loop and start fresh")
        if name == "note":
            p.add_argument("--file", required=True, help="Claude's review JSON (verdict.schema.json)")
    args = ap.parse_args()
    {"review": cmd_review, "build": cmd_build, "note": cmd_note,
     "status": cmd_status, "watch": cmd_watch}[args.cmd](args)


if __name__ == "__main__":
    main()
