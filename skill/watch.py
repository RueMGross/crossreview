#!/usr/bin/env python3
"""Live dashboard for a crossreview loop: who is doing what, results, disagreements, next step.

  watch.py --dir <project>          full screen, refreshes twice a second (Ctrl-C quits)
  watch.py --dir <project> --once   print a single frame (testing)

Reads <project>/.crossreview/{state.json, activity.json, live.log} written by crossreview.py,
plus the Claude Code session transcript for Claude's latest actions. Paints its own white
background; restarts itself when this file changes.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import sys
import textwrap
import time
from pathlib import Path

if os.name == "nt":  # enable ANSI/VT processing so the dashboard renders in any Windows console
    try:
        import ctypes
        _k = ctypes.windll.kernel32
        _k.SetConsoleMode(_k.GetStdHandle(-11), 7)  # PROCESSED | WRAP | VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        pass

SD_NAME = ".crossreview"
ESC = "\x1b["
# 256-colour codes for a white background
PALETTE = {"claude": 166, "gemini": 26, "you": 91, "ok": 28, "bad": 160, "warn": 136, "dispute": 163,
           "text": 236, "head": 232, "dim": 244, "faint": 250, "white": 231}
BASE = f"{ESC}0m{ESC}48;5;231m{ESC}38;5;{PALETTE['text']}m"  # white page, dark text
AGENT = {"Claude": "claude", "Gemini": "gemini", "You": "you"}
SEV = {"blocker": "bad b", "major": "warn b", "minor": "dim"}
STATUS = {  # ledger status -> (label, style)
    "open": ("open", "bad"),
    "reopened": ("fix not accepted", "bad b"),
    "fixed": ("fixed, awaiting re-check", "warn"),
    "rebutted": ("rebutted, awaiting reply", "dispute"),
    "disputed": (" ◆ DISPUTED ", "white b on-dispute"),
    "escalated": (" ▲ YOUR CALL ", "white b on-you"),
    "resolved": ("✓ resolved", "ok b"),
    "accepted": ("✓ rebuttal accepted", "ok b"),
    "deferred": ("deferred", "dim"),
    "dropped": ("dropped by reviewer", "dim"),
}
OPEN = ("open", "reopened", "fixed", "rebutted", "disputed", "escalated")
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def paint(text, spec):
    if not spec:
        return text
    codes = ""
    for t in spec.split():
        if t == "b":
            codes += ESC + "1m"
        elif t.startswith("on-"):
            codes += f"{ESC}48;5;{PALETTE[t[3:]]}m"
        else:
            codes += f"{ESC}38;5;{PALETTE[t]}m"
    return codes + text + BASE


def chip(text, colour):
    return f" {text} ", f"white b on-{colour}"


def plain(text):
    return str(text).replace("`", "").replace("\n", " ")


class Screen:
    def __init__(self, width):
        self.w, self.lines = width, []

    def add(self, *segs, right=()):
        """One line from (text, style) segments, cut to the screen width; `right` is right-aligned."""
        segs = [s if isinstance(s, tuple) else (s, "") for s in segs]
        right = [s if isinstance(s, tuple) else (s, "") for s in right]
        rlen = sum(len(t) for t, _ in right)
        if sum(len(t) for t, _ in segs) + rlen + 2 > self.w:
            right, rlen = [], 0  # too narrow: the left part matters more
        room, out = self.w - (rlen + 1 if right else 0), [BASE]
        for text, spec in segs:
            if room <= 0:
                break
            if len(text) > room:
                text = text[:room - 1] + "…"
            room -= len(text)
            out.append(paint(text, spec))
        if right:
            out.append(" " * (room + 1) + "".join(paint(t, s) for t, s in right))
        self.lines.append("".join(out))

    def section(self, title, spec="head b", extra=""):
        left, mid = f"▌ {title} ", f"{extra} " if extra else ""
        self.add()
        self.add((left, spec), (mid, "dim"), ("─" * max(self.w - len(left) - len(mid), 0), "faint"))


def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def parse_ts(s):
    try:
        t = datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def dur(seconds):
    s = int(max(seconds, 0))
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}h{s % 3600 // 60:02d}"


def k(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k"


def files(n):
    return f"{n} file{'s' if n != 1 else ''}"


def wrap(text, width, max_lines=2):
    out = textwrap.wrap(text, max(width, 10)) or [""]
    if len(out) > max_lines:
        out = out[:max_lines]
        out[-1] = out[-1][:max(width - 1, 0)] + "…"
    return out


def tail_lines(path, max_bytes=200_000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, TypeError):
        return []
    return lines[1:] if size > max_bytes else lines  # first line may be cut


def claude_actions(path, since, n=3):
    """Claude's latest tool calls from the Claude Code transcript (newest last)."""
    acts = []
    for line in tail_lines(path, 600_000) if path else []:
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "assistant":
            continue
        t = parse_ts(e.get("timestamp"))
        if not t or (since and t <= since):
            continue
        for c in (e.get("message") or {}).get("content") or []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                inp = c.get("input") or {}
                what = inp.get("description") or inp.get("file_path") or inp.get("pattern") or inp.get("command") or ""
                acts.append((t, c.get("name", "?"), str(what).replace(str(Path.home()), "~")))
    return acts[-n:]


def fid_key(fid):
    return tuple(int(x) for x in re.findall(r"\d+", fid)) or (0,)


# ---------- the stage: two ASCII robots passing work back and forth ----------

ANTENNA = {"Claude": [("\\|/", "-*-"), (" | ", "-+-")],   # 8-point burst, twinkles when active
           "Gemini": [(" | ", "-+-"), (" ' ", "-|-")]}   # 4-point sparkle
SCAN = ["o   o  ", " o   o ", "  o   o", " o   o "]
FACES = {"work": (" o   o ", "  \\_/  "), "wait": (" -   - ", "  ___  "), "happy": (" ^   ^ ", " \\___/ "),
         "grumpy": (" >   < ", "  ~~~  "), "puzzled": (" o   O ", "   o   ")}
STATE_WORD = {"work": "writing", "review": "reviewing", "wait": "waiting", "happy": "agreed",
              "grumpy": "unconvinced", "puzzled": "stuck"}


def robot(agent, state, tick):
    """Six 11-char lines: antenna, head with a face for the state."""
    a1, a2 = ANTENNA[agent][tick % 2 if state in ("work", "review") else 0]
    eyes, mouth = (SCAN[tick % 4], "  ---  ") if state == "review" else FACES[state]
    if state == "work" and tick % 9 == 0:
        eyes = " -   - "  # blink
    return [f"    {a1}    ", f"    {a2}    ", " ╭───────╮ ", f" │{eyes}│ ", f" │{mouth}│ ", " ╰───────╯ "]


def prop(state, colour, tick, mirror):
    """Six 8-char cells beside a robot: a page being written or scanned, floating z's, or a mark."""
    blank = [("        ", "")]
    cells = [blank[:] for _ in range(6)]
    pad = (lambda s: s.rjust(8)) if mirror else (lambda s: s.ljust(8))
    if state in ("work", "review"):
        fill = tick % 13
        cells[1] = [(" ┌────┐ ", "dim")]
        for row in range(3):
            if state == "work":
                text = "~" * max(0, min(4, fill - row * 4))
                spec = colour
            else:
                text, spec = "~~~~", (colour + " b") if row == tick % 3 else "faint"
            reading = state == "review" and row == tick % 3
            cell = f" │{text:<4}│{'<' if reading else ' '}" if mirror else f"{'>' if reading else ' '}│{text:<4}│ "
            cells[2 + row] = [(cell[:2], "dim"), (cell[2:6], spec), (cell[6:], "dim")]
        cells[5] = [(" └────┘ ", "dim")]
    elif state == "wait":
        p = (tick // 2) % 3  # a z drifting up and away from the head
        z = "zZZ"[p]
        cells[4 - p] = [((z + " " * (1 + p)).rjust(8) if mirror else (" " * (1 + p) + z).ljust(8), "dim")]
    elif state in ("happy", "puzzled", "grumpy"):
        mark, spec = {"happy": (" ✓ ", "ok b"), "puzzled": (" ? ", "you b"), "grumpy": (" ! ", "dispute b")}[state]
        cells[3] = [(pad(mark), spec)]
    return cells


def conveyor(kind, label, sub, tick, width):
    """Six cells between the robots: an animated hand-off arrow, or the final outcome."""
    cells = [[(" " * width, "")] for _ in range(6)]
    if kind in ("right", "left"):
        track = ["─"] * width
        span = width - 3
        pos = (tick * 2) % span
        if kind == "right":
            track[-1], packet = "▶", 1 + pos
        else:
            track[0], packet = "◀", width - 2 - pos
        cells[3] = [("".join(track[:packet]), "faint"), ("●", "head b"), ("".join(track[packet + 1:]), "faint")]
        cells[2] = [(label.center(width), "text b")]
        cells[4] = [(sub.center(width), "dim")]
    else:
        text, spec = (" ✓ AGREED ", "white b on-ok") if kind == "done" else (" ▲ OVER TO YOU ", "white b on-you")
        side = max(width - len(text), 0)
        cells[3] = [("═" * (side // 2), "faint"), (text, spec), ("═" * (side - side // 2), "faint")]
        cells[4] = [(label.center(width), "dim")]
    return cells


def stage(s, st, act, led, tick):
    claude_author = st["role"] == "claude-author"
    roles = {"Claude": "author" if claude_author else "reviewer", "Gemini": "reviewer" if claude_author else "author"}
    who, done = act.get("who"), act.get("done")
    disputed = any(e["status"] in ("disputed", "escalated") for e in led.values())
    states = {}
    for agent in ("Claude", "Gemini"):
        if done or who == "You":
            states[agent] = "puzzled" if who == "You" else "happy"
        elif who == agent:
            states[agent] = "work" if roles[agent] == "author" else "review"
        else:
            states[agent] = "grumpy" if disputed and roles[agent] == "reviewer" else "wait"

    rnd = act.get("round") or st["round"]
    n_open = sum(e["status"] in OPEN for e in led.values())
    if done or who == "You":
        kind = "done" if who != "You" else "you"
        label = f"after {rnd} round{'s' if rnd != 1 else ''}"
        sub = ""
    else:
        kind = "right" if who == "Gemini" else "left"
        if roles.get(who) == "reviewer":
            label = "work + brief" if rnd <= 1 else "fixes + replies"
        else:
            label = f"{n_open} finding{'s' if n_open != 1 else ''}" if n_open else "task"
        sub = f"round {rnd}"

    width = max(min(s.w - 48, 46), 16)
    inner = 42 + width  # 11 head + 8 prop + 2 + belt + 2 + 8 prop + 11 head
    ind = " " * max((s.w - inner) // 2, 2)  # centred
    heads = {a: robot(a, states[a], tick) for a in ("Claude", "Gemini")}
    props = {a: prop(states[a], AGENT[a], tick, mirror=(a == "Gemini")) for a in ("Claude", "Gemini")}
    belt = conveyor(kind, label, sub, tick, width)
    for i in range(6):
        segs = [(ind, "")]
        for agent in ("Claude", "Gemini"):
            active = states[agent] in ("work", "review")
            ant = (AGENT[agent] + (" b" if tick % 2 else "")) if active else "faint"
            head_spec = AGENT[agent] + (" b" if active else "")
            part = [(heads[agent][i], ant if i < 2 else head_spec)]
            segs += part + props[agent][i] if agent == "Claude" else props[agent][i] + part
            if agent == "Claude":
                segs += [("  ", "")] + belt[i] + [("  ", "")]
        s.add(*segs)
    # name chips centred under the heads, then role + what each is doing
    s.add((ind + " ", ""), chip("CLAUDE", "claude"), (" " * (inner - 19), ""), chip("GEMINI", "gemini"))
    dots = ("." * (tick % 4)).ljust(3)
    words = {a: STATE_WORD[states[a]] + (dots if states[a] in ("work", "review") else "") for a in ("Claude", "Gemini")}
    left, right = f"{roles['Claude']} · {words['Claude']}", f"{roles['Gemini']} · {words['Gemini']}"
    s.add((ind, ""), (left, "claude"), (" " * max(inner - len(left) - len(right), 1), ""), (right, "gemini"))


def frame(d, width, height, tick):
    sd = d / SD_NAME
    st = load_json(sd / "state.json", None)
    act = load_json(sd / "activity.json", {})
    s = Screen(width)
    if not st:
        s.add(("  Waiting for the first crossreview round in ", "dim"), (str(d), "text"))
        return s.lines
    now = datetime.datetime.now(datetime.timezone.utc)
    author, reviewer = ("Claude", "Gemini") if st["role"] == "claude-author" else ("Gemini", "Claude")
    who = act.get("who", "Claude")
    rnd = act.get("round") or st["round"]
    led = st.get("ledger", {})

    # header: title, scope, round progress dots, roles
    dots = []
    for i in range(1, st["max_rounds"] + 1):
        if i <= st["round"]:
            dots.append(("● ", "head"))
        elif i == rnd and act.get("running"):
            dots.append(("● ", AGENT.get(who, "head")))
        else:
            dots.append(("○ ", "faint"))
    s.add()
    s.add(("  ", ""), chip("CROSS-REVIEW", "head"), (f"  {d.name}", "head b"), (f"   {', '.join(st['scope'])}", "dim"),
          right=[("round  ", "dim")] + dots + [(f" {rnd} of {st['max_rounds']}  ", "head b")])
    s.add()
    s.add(("  author ", "dim"), chip(author, AGENT[author]), ("    reviewer ", "dim"), chip(reviewer, AGENT[reviewer]),
          (" read-only" if reviewer == "Gemini" else "", "dim"), ("    tie-breaker ", "dim"), chip("You", "you"),
          right=[(f"{st['model']}  ", "dim")])

    # now + next
    s.section("NOW")
    s.add()
    stage(s, st, act, led, tick)
    s.add()
    since = parse_ts(act.get("since"))
    elapsed = f"   {dur((now - since).total_seconds())}" if since else ""
    if act.get("done"):
        if who == "You":
            s.add(("  ", ""), chip("▲ YOUR CALL", "you"), ("  " + act.get("what", ""), "you b"))
        else:
            s.add(("  ", ""), chip("✓ DONE", "ok"), ("  " + act.get("what", ""), "ok b"))
    else:
        col = AGENT.get(who, "head")
        s.add(("  ", ""), chip(f"{SPIN[tick % len(SPIN)]} {who.upper()}", col), ("  " + act.get("what", ""), "head b"),
              (elapsed, "dim"))
        if act.get("running"):
            steps = [l for l in tail_lines(sd / "live.log", 50_000) if l.startswith("  > ")]
            for l in steps[-3:]:
                s.add(("     │ ", "faint"), (plain(l[4:]), "gemini"))
        else:
            for t, name, what in claude_actions(st.get("claude_transcript"), since):
                s.add(("     │ ", "faint"), (t.astimezone().strftime("%H:%M:%S  "), "dim"), (f"{name}  ", "claude b"),
                      (plain(what), "text"))
    nx = st.get("next_step") or {}
    if nx:
        s.add(("  ▶ next   ", "dim"), (nx.get("who", ""), AGENT.get(nx.get("who"), "head") + " b"),
              ("  " + nx.get("what", ""), "text"))

    # rounds timeline
    s.section("ROUNDS")
    toks = st.get("tokens", {})
    for r in st.get("rounds", []):
        rw = r["who"]
        segs = [(f"  R{r['round']}   ", "head b"), (f"{rw:<8}", AGENT[rw] + " b")]
        if r["kind"] == "review":
            c = r.get("counts") or {}
            ok = r.get("verdict") == "APPROVE"
            segs += [("reviewed   ", "text"), chip(r.get("verdict", "?"), "ok" if ok else "bad"), ("   ", ""),
                     (f"{c.get('blocker', 0)} blocker", "bad b" if c.get("blocker") else "dim"), (" · ", "faint"),
                     (f"{c.get('major', 0)} major", "warn b" if c.get("major") else "dim"), (" · ", "faint"),
                     (f"{c.get('minor', 0)} minor", "dim")]
        elif r["kind"] == "build":
            segs.append(("built the work + wrote the brief" if rw == "Claude" else f"built · {files(r.get('changed', 0))} changed", "text"))
        else:
            rep = r.get("replies") or {}
            segs += [("replied    ", "text"), (f"{rep.get('fixed', 0)} fixed", "ok b" if rep.get("fixed") else "dim"),
                     (" · ", "faint"), (f"{rep.get('rebutted', 0)} rebutted", "dispute b" if rep.get("rebutted") else "dim"),
                     (" · ", "faint"), (f"{rep.get('deferred', 0)} deferred", "dim"),
                     (f"   {files(r.get('changed', 0))} changed", "dim")]
        if r.get("secs"):
            segs.append((f"   {dur(r['secs'])}", "dim"))
        u = (toks.get(str(r["round"]), {}).get(r.get("tok")) or {}).get("usage")
        tok = f"in {k(u['in'])} + {k(u['cached'])} cached · out {k(u['out'])}  " if u and (u.get("in") or u.get("out")) else ""
        s.add(*segs, right=[(tok, "dim")])
    if not st.get("rounds"):
        s.add(("  nothing yet", "dim"))
    totals = {}
    for per_round in toks.values():
        for agent, rec in per_round.items():
            for key, val in (rec.get("usage") or {}).items():
                acc = totals.setdefault(agent.split()[0], {})
                acc[key] = acc.get(key, 0) + val
    if totals:
        segs = [("  Σ tokens   ", "dim")]
        for agent in ("Claude", "Gemini"):
            u = totals.get(agent)
            if u:
                segs += [(agent, AGENT[agent] + " b"),
                         (f"  in {k(u.get('in', 0))} + {k(u.get('cached', 0))} cached · out {k(u.get('out', 0))}    ", "text")]
        s.add()
        s.add(*segs)

    # findings ledger: open ones first, issue text wrapped underneath
    n_open = sum(e["status"] in OPEN for e in led.values())
    s.section("FINDINGS", extra=f"{len(led)} total · {n_open} open")
    if not led:
        s.add(("  none yet", "dim"))
    rows = sorted(led.items(), key=lambda kv: (kv[1]["status"] not in OPEN, fid_key(kv[0])))
    for fid, e in rows[:8]:
        label, spec = STATUS.get(e["status"], (e["status"], "text"))
        if e.get("closed") and e["status"] in ("resolved", "accepted"):
            label += f" in R{e['closed']}"
        s.add((f"  {fid:<7}", "head b"), (f"{e.get('sev', '?'):<9}", SEV.get(e.get("sev"), "text")),
              (label, spec), (" " * max(27 - len(label), 1), ""), (e.get("where", ""), "dim"))
        for line in wrap(plain(e.get("issue", "")), width - 12):
            s.add(("         ", ""), (line, "text"))
    if len(rows) > 8:
        s.add((f"  … {len(rows) - 8} more in REVIEW_LOG.md", "dim"))

    # disagreements: anything rebutted, kept after a rebuttal, or whose fix was not accepted
    contested = [(fid, e) for fid, e in sorted(led.items(), key=lambda kv: fid_key(kv[0]))
                 if e["status"] in ("disputed", "escalated", "reopened", "rebutted")
                 or any(ev[2].startswith(("rebutted", "kept it", "fix not accepted")) for ev in e["events"])]
    live = any(e["status"] in ("disputed", "escalated", "reopened", "rebutted") for _, e in contested)
    s.section("DISAGREEMENTS", "dispute b" if live else "head b")
    if not contested:
        done = led and not n_open
        s.add(("  none: every finding was fixed and confirmed", "ok") if done else ("  none so far", "dim"))
    for fid, e in contested[:4]:
        label, spec = STATUS.get(e["status"], (e["status"], "text"))
        s.add((f"  {fid}  ", "head b"), (label, spec), (f"   {plain(e.get('issue', ''))}", "dim"))
        for ev_round, ev_who, text in e["events"][-4:]:
            s.add((f"      R{ev_round}  ", "dim"), (f"{ev_who:<8}", AGENT.get(ev_who, "head") + " b"), (plain(text), "text"))

    # Gemini's live output fills the rest of the screen
    room = height - len(s.lines) - 4
    if room >= 2:
        s.section("GEMINI OUTPUT", "gemini b", extra="live")
        lines = [plain(l) for l in tail_lines(sd / "live.log", 100_000) if l.strip()]
        for l in lines[-room:]:
            spec = ("gemini b" if l.startswith("=====") else "dim" if l.startswith("  > ")
                    else "head b" if l.startswith("VERDICT") else "warn" if l.startswith("  [R") else "text")
            s.add(("  " + l, spec))
    s.lines = s.lines[:height - 2]
    s.add()
    s.add((f"  Ctrl-C closes this window · full log: {sd / 'REVIEW_LOG.md'}", "dim"))
    return s.lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=".")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--width", type=int, default=132)
    ap.add_argument("--height", type=int, default=52)
    args = ap.parse_args()
    d = Path(args.dir).expanduser().resolve()
    if args.once:
        print("\n".join(frame(d, args.width, args.height, 0)) + ESC + "0m")
        return
    me = Path(__file__).resolve()
    mtime = me.stat().st_mtime
    sys.stdout.write(f"{ESC}?1049h{ESC}?25l")  # alternate screen, hide cursor
    tick = 0
    try:
        while True:
            if me.stat().st_mtime != mtime:  # code changed: restart with the new version
                sys.stdout.write(f"{ESC}0m{ESC}?25h{ESC}?1049l")
                sys.stdout.flush()
                os.execv(sys.executable, [sys.executable, str(me)] + sys.argv[1:])
            cols, rows = shutil.get_terminal_size((132, 52))
            try:
                lines = frame(d, cols, rows, tick)
            except Exception as exc:  # a half-written file or odd state must not kill the window
                lines = [BASE + f"  dashboard error: {exc!r} (retrying)"]
            sys.stdout.write(f"{ESC}H" + f"{ESC}K\r\n".join(lines) + f"{ESC}K\r\n{BASE}{ESC}J")
            sys.stdout.flush()
            tick += 1
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write(f"{ESC}0m{ESC}?25h{ESC}?1049l")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
