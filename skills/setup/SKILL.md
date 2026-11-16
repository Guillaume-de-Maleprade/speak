---
name: setup
description: Install the Python dependencies of the speak MCP server and download its voice model, so the speak tool can start. Use when the user runs /speak:setup, or when the speak MCP server fails to connect with a missing-module error.
---

The speak MCP server starts with the `python` on PATH and needs `mcp` and `pocket-tts` installed in that same interpreter. Install them, warm the model cache, then reconnect the server.

Tell the user before step 2 that it downloads torch (several hundred MB) and takes 1-3 minutes.

1. **Find the interpreter.** Run `python -c "import sys; print(sys.executable, sys.version)"`. If `python` is missing or older than 3.10, stop: tell the user to install Python 3.10+ and put it on PATH, then rerun `/speak:setup`.
2. **Install.** Run `python -m pip install "mcp>=2.3,<3" "pocket-tts>=4.0,<5"` (same pins as the plugin's `pyproject.toml`; use `python -m pip`, never bare `pip`, so it lands in the interpreter the server uses). On a permissions error, retry with `--user`.
3. **Check imports.** Run `python -c "import mcp, pocket_tts; print('ok')"`. On failure, show the error and stop.
4. **Warm the models.** Ask the user which languages they want the agent to speak, with the host's question tool (Claude Code: `AskUserQuestion`, `multiSelect: true`; Codex: `request_user_input`). Options: French, English, Spanish, German; the question names Italian, Portuguese and Dutch as choices to type under Other. Then download each picked language's weights now, so the first `speak` call in it does not wait. Names: `fr`→`french`, `en`→`english`, `de`→`german`, `es`→`spanish`, `it`→`italian`, `pt`→`portuguese`, `nl`→`dutch`. Run once per language:
   `python -c "from pocket_tts import TTSModel; TTSModel.load_model(language='<name>')"`
5. **Reconnect.** Tell the user to run `/mcp` and reconnect `plugin:speak:speak` (or restart the session). Then call `speak` once per picked language with a short test sentence in that language, to confirm the user hears it.

Report what was installed (package versions from step 2), which languages were downloaded, and whether each test sentence played.
