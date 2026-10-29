---
name: crossreview
description: Critical-review loop between Claude (author) and Google Gemini via the Antigravity CLI `agy` (reviewer, read-only). Claude builds, Gemini reviews, Claude fixes or rebuts, repeat until Gemini approves or the round limit is hit. Use when the user types /crossreview or asks to "have Gemini review/check this", "cross-review", "get a second opinion from Gemini/Antigravity", or wants two AIs to check each other's work. Also supports Gemini as author with Claude reviewing (--author gemini).
argument-hint: "[task or files] [--max-rounds N] [--model ID] [--author gemini] [--no-watch]"
---

# Cross-review: Claude <-> Gemini (Antigravity)

Helper: `python3 ~/.claude/skills/crossreview/crossreview.py <review|build|note|status|watch> --dir <DIR> ...`
Run every helper call with Bash `timeout: 600000` (a round takes 1 to 9 min). For big scopes pass `--timeout 1800` and use `run_in_background: true`.

## Facts
- Reviewer = `agy` (Antigravity CLI). The Gemini CLI does NOT work for this account (IneligibleTierError, migrated to Antigravity).
- `agy` also offers Claude models. Always use a Gemini model (default `gemini-4-argon-high`, list with `agy models`). The script warns if a non-Gemini model ran.
- Gemini runs in `--mode plan` (read-only). The script checks file fingerprints before and after and warns if anything changed.
- State goes to `<DIR>/.crossreview/` (auto-added to `.git/info/exclude` in git repos):
  - `REVIEW_LOG.md`: readable transcript of every round
  - `live.log`: streamed Gemini output
  - snapshots, plus per-round prompt/result files
- **Dashboard:** round 1 opens a white Terminal window running `watch.py`. It shows:
  - NOW: who is working, with their last actions
  - NEXT: the next step and who takes it
  - ROUNDS: verdicts and tokens per round
  - FINDINGS: each finding's status (open, fixed, rebutted, disputed, resolved)
  - DISAGREEMENTS: the full back-and-forth on contested points
  - GEMINI OUTPUT: the live stream

  `--no-watch` disables the window, `watch --dir DIR` reopens it, and it reloads itself when `watch.py` changes. The dashboard tracks your replies only if the response file uses exactly `R1-2 FIXED: ...` / `R1-2 REBUTTED: ...` / `R1-2 DEFERRED: ...` lines.
- **Tokens:** every helper call prints `TOKENS` lines. Gemini's come from `agy`'s usage report; Claude's come from this session's transcript (`$CLAUDE_CODE_SESSION_ID`), counted since the last helper call (round 1: since the user's last prompt). `status` prints per-round numbers and totals.

## Defaults and how to change them
The loop runs well as-is: **max 4 rounds, reviewer `gemini-4-argon-high`, live dashboard on.** Two ways to change it:
- **This run only** — flags: `--max-rounds N`, `--model <id>` (`agy models` lists them), `--no-watch`, `--author gemini`.
- **Every run** — a small JSON file at `~/.claude/crossreview.json`. Keys, all optional:
  - `"max_rounds"`: integer (default 4)
  - `"model"`: agy model id (default `gemini-4-argon-high`; keep it a Gemini model)
  - `"watch"`: `false` to default the dashboard off

  A flag always overrides the file for that run. Example: `{ "max_rounds": 6, "watch": false }`.

Changes apply to new loops; a loop already running keeps the settings it started with.

## Bare `/crossreview` (no task given)
Review the work already done in this session. Take the files created or changed in this conversation as `--scope`, and the project folder as `--dir`. Write the brief from the conversation: what the user asked for, what was done, which checks ran and their output. Skip step 1 (build) and start at step 2. If the work exists only as chat text (no files), first write it to a file in the scratchpad, then review that file. If it's unclear which files belong to the work, list your best guess in one line and go ahead.

## Before starting
- Everything in scope is sent to Google. Ask first if the scope may hold patient data, unblinding keys, credentials or unpublished data the user hasn't cleared. On `/Volumes/Department` only use paths the user named.
- Pick `--dir`: the project root or the folder that holds the work. Pick `--scope`: only the files/dirs under review. Default is the whole folder; keep it tight so reviews stay fast and focused.
- If a `.crossreview/` from an earlier, different task exists, pass `--new` (archives the old loop).

## Default flow: Claude authors, Gemini reviews
1. **Build** the work (skip if the user only wants existing work reviewed). Run whatever checks exist (tests, running the script, rendering the figure) and keep the output.
2. **Brief** (write to the scratchpad, e.g. `crossreview-brief.md`):
   - the goal/spec in the user's words
   - what was done
   - how to verify it
   - check output, trimmed
   - known limitations

   State facts. Don't argue that the work is correct, and don't paste whole files: Gemini reads them from disk.
3. **Review round:** `crossreview.py review --dir DIR --scope ... --brief BRIEF [--max-rounds 4]`
4. **Show the user** the round's result in chat every time: verdict, then one bullet per finding (id, severity, where, issue), then the two `TOKENS` lines (Claude's work since the last step, Gemini's review). The user wants to see what Gemini said, not only your conclusion.
5. **Handle each blocker/major finding on its merits.** Verify it yourself first (read the code, reproduce, calculate). Then either:
   - `FIXED`: say what changed, or
   - `REBUTTED`: give concrete counter-evidence.

   Don't accept a finding just because the reviewer said so, and don't rebut just to end the loop. Minor findings: fix if cheap and clearly right, else `DEFERRED`.
6. **Re-run your checks**, then write the response file:
   - one line per id: `R1-2 FIXED: ...`, `R1-3 REBUTTED: <evidence>`
   - the new check output

   Next round: `crossreview.py review --dir DIR --brief RESPONSE` (scope, model and conversation are remembered; the diff since the last round is added automatically).
7. **Follow the script's `NEXT:` line:**
   - `CONTINUE`: back to step 5.
   - `DONE`: finish.
   - `STOP (max rounds)`: finish and hand the open findings to the user.
   - `ESCALATE` ids (same finding blocking for 3 rounds): stop arguing that point and put both positions to the user.
8. **Final report in chat:**
   - on the user's **first run** (no `~/.claude/crossreview.json` yet), add one short line: the defaults (max 4 rounds, Gemini reviewer, live dashboard) and that they can change them per run with `--max-rounds N` / `--no-watch`, or for every run via `~/.claude/crossreview.json`
   - number of rounds
   - what changed
   - findings fixed / rebutted / deferred
   - any unresolved disagreement, with both sides' arguments
   - path to `REVIEW_LOG.md`

## Swapped roles: Gemini authors, Claude reviews (`--author gemini`)
1. Write the task brief, then run `crossreview.py build --dir DIR --scope ... --brief BRIEF`. Gemini edits files in accept-edits mode.
2. Review critically, applying the reviewer rules in `CHARTER` (top of `crossreview.py`) to yourself. Read the diff (`.crossreview/round-NN.diff`) and the files, and run checks yourself.
3. Write your review as JSON matching `verdict.schema.json` (ids `R<round>-<n>`, evidence required), then run `crossreview.py note --dir DIR --file REVIEW.json`.
4. If NEXT is CONTINUE, run `crossreview.py build --dir DIR`. Your findings are passed to Gemini automatically; add `--brief` only for extra instructions.
5. Same stop rules and final report as above.

## Rules
- One editor at a time. Never edit files while a review round runs.
- If the script prints `WARNING: files changed during the read-only review`, show the user the diff and ask before keeping or reverting.
- If the reviewer returns nothing parsable or `agy` fails, check `.crossreview/live.log` and `agy-stderr.log`. If auth expired, ask the user to run `! agy` once to log in.
