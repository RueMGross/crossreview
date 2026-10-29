# Install crossreview (instructions for Claude Code to follow)

You are Claude Code, setting up the **crossreview** skill for the user who just asked you to install it. Work through these steps in order, running the commands yourself. Keep the user informed in short lines; only pause when a step genuinely needs them (the Google sign-in).

Repo raw base: `https://raw.githubusercontent.com/RueMGross/crossreview/main`

## 1. Detect the platform and Python
- Determine the OS (Windows, macOS, or Linux).
- Confirm Python 3 is available: `python --version` (Windows) or `python3 --version` (macOS/Linux). If missing, tell the user to install Python 3 from python.org (Windows: tick "Add to PATH") and stop.

## 2. Make sure the reviewer CLI `agy` (Google Antigravity) is installed
Check first: `agy --version`. If that works, skip to step 3. If not, install it:
- **Windows:** `winget install Google.AntigravityCLI`  (if winget is unavailable, run in PowerShell: `irm https://antigravity.google/cli/install.ps1 | iex`)
- **macOS / Linux:** `curl -fsSL https://antigravity.google/cli/install.sh | sh`

After installing, `agy` may only be on PATH in a **new** terminal. If `agy --version` still fails, tell the user to open a fresh terminal and re-run the install request.

## 3. Create the skill folder and download the files
Target folder:
- Windows: `%USERPROFILE%\.claude\skills\crossreview`
- macOS/Linux: `~/.claude/skills/crossreview`

Create it, then download these four files from `<raw base>/skill/` into it (preserve names):
`SKILL.md`, `crossreview.py`, `watch.py`, `verdict.schema.json`

Use `curl` (ships with Windows 10+, macOS, Linux):
```
curl -fsSL <raw base>/skill/SKILL.md            -o <folder>/SKILL.md
curl -fsSL <raw base>/skill/crossreview.py      -o <folder>/crossreview.py
curl -fsSL <raw base>/skill/watch.py            -o <folder>/watch.py
curl -fsSL <raw base>/skill/verdict.schema.json -o <folder>/verdict.schema.json
```
If `curl` is missing on Windows, use PowerShell instead, e.g.
`iwr <raw base>/skill/crossreview.py -OutFile <folder>\crossreview.py`.

Verify all four files exist and are non-empty.

## 4. Sign the user in to Google (one time, interactive)
Tell the user to run `agy` **once** in their terminal, pick their Google account, and choose a Gemini model (the loop defaults to `gemini-4-argon-high`). Headless review runs reuse that cached login. This is the only manual step.

## 5. Confirm it works
Tell the user setup is done. To use it, in any project folder they can:
- type `/crossreview` (review the work just done), or
- say *"have Gemini review this"* / `/crossreview build X and have Gemini check it`.

Mention the defaults (max 4 rounds, reviewer `gemini-4-argon-high`, live dashboard) and that they can change them per run (`--max-rounds N`, `--no-watch`) or for every run by creating `~/.claude/crossreview.json`, e.g. `{ "max_rounds": 6, "watch": false }`.

**Privacy reminder to pass on:** files under review are sent to both Anthropic and Google. No patient data, unblinding keys, credentials or unpublished data without clearance.
