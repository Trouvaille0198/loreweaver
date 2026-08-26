# Implemented: player-facing story hint command

- **Problem:** A table can reach a point where players know the scene but cannot identify a useful next action.
- **Shape:** `.hint [focus]` and the Chinese aliases `.提示`, `.卡住`, and `.推进` prepare a localized request and hand it to the ordinary Keeper turn pipeline. The command is available to players, broadcasts the resulting Keeper narrative, and retains the normal turn lock, tool access, history, usage accounting, Scribe pass, and companion pacing.
- **Safety:** The request asks for a small spoiler-safe lead or one actionable next step. Keeper-only secrets, hidden truth, unrevealed clues, and future outcomes remain protected by the Keeper system prompt. The command itself starts no separate model-call lane.
- **Rule home:** `gateway/commands/plot.py`, `gateway/commands/types.py`, and `gateway/turn.py`; both dialects live in `locales/{en,zh}/commands.json`.
