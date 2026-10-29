# crossreview

**Two AIs peer-reviewing each other.** Claude Code does the work; Google Gemini (via the Antigravity CLI `agy`) reviews it critically, read-only. Claude fixes or rebuts each finding, Gemini re-checks, and the loop repeats until Gemini approves or a stuck point is escalated to you. A live terminal dashboard shows both sides working. Roles can be swapped (`--author gemini`).

Different vendors have different blind spots, so a disagreement is informative, much like peer review. It also makes the obvious point that passing tests ≠ correct: the reviewer tests the inputs your tests missed.

It's a single Claude Code **skill** — one `SKILL.md` plus ~600 lines of dependency-free Python. No servers, no API keys, no MCP. Runs on **your** accounts: your Anthropic subscription for Claude Code, your own Google account for the reviewer.

## Requirements
- [Claude Code](https://claude.com/claude-code) (any subscription)
- [Antigravity CLI](https://antigravity.google) `agy`, signed in to a Google account — the older Gemini CLI no longer works for individual accounts
- Python 3 (standard library only)
- Windows, macOS, or Linux. The live dashboard opens a new console window (Windows Terminal / conhost, Terminal.app, or an X11 terminal); without one, pass `--no-watch`.

## Install
In **Claude Code**, paste one line and let it run:

> Set up the crossreview tool: fetch `https://raw.githubusercontent.com/RueMGross/crossreview/main/INSTALL.md` and follow every step.

Claude installs `agy` and the skill, then asks you to sign in to Google once. (The one-line install needs the repo to be **public**; while it's private, clone it and copy `skill/` into `~/.claude/skills/crossreview/` yourself.)

## Use
In any project folder:
- `/crossreview` — review the work just done this session
- `/crossreview build X and have Gemini review it`
- `/crossreview review path/to/file.py` — review existing work only
- `/crossreview --author gemini <task>` — Gemini builds, Claude reviews
- or just say *"have Gemini check this"*

## Changing the defaults
Defaults: **max 4 rounds**, reviewer **`gemini-4-argon-high`**, **live dashboard on**.

- Per run: `--max-rounds N`, `--model <id>` (`agy models` lists them), `--no-watch`.
- For every run: create `~/.claude/crossreview.json`, e.g.
  ```json
  { "max_rounds": 6, "model": "gemini-4-argon-high", "watch": false }
  ```
  A flag overrides the file for that run. Changes apply to new loops; a running loop keeps its settings.

## How the rules are enforced
| Rule | How | Hard/soft |
|---|---|---|
| Reviewer cannot edit files | `agy --mode plan` + before/after file fingerprint check | hard |
| Reviewer answers in a fixed format (verdict, findings, severity, evidence) | `--json-schema verdict.schema.json` | hard |
| Reviewer is really Gemini | `--model gemini-4-argon-high`, warns otherwise | hard |
| Max rounds; escalate a point stuck 3 rounds to the human | Python | hard |
| Verdict follows from severities, not the reviewer's word | script recomputes | hard |
| Evidence for every finding; don't invent issues; don't approve to be agreeable | reviewer prompt | soft |

## Privacy
Files under review are sent to both Anthropic and Google. Do not include patient data, unblinding keys, credentials or unpublished data without clearance.

## License
MIT — see [LICENSE](LICENSE).
