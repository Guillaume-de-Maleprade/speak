# speak-mcp

An MCP server that lets a coding agent speak out loud, offline, on the CPU.
It's part of [Voice Coding](https://github.com/Guillaume-de-Maleprade/internal-OS): coding agents for people who can't see the screen.

- One tool: `speak(text, lang, wait)`.
- Text-to-speech runs in-process with [Kyutai Pocket TTS](https://github.com/kyutai-labs/pocket-tts): no API key, no HTTP server.
- `lang` is `fr`, `en`, `de`, `es`, `it`, `pt` or `nl`. Each language model loads on first use, so the agent picks the language on every call.
- Calls play in order, so two sentences never overlap, even across sessions: a machine-wide named mutex lets one `speak-mcp` process play at a time. `wait: false` (the default) returns at once and the agent keeps working.
- Only the main agent speaks. The server instructions tell subagents and delegated tasks to report to their parent instead.
- Windows only for now: playback uses `winsound`.

## Install

Install the plugin in Claude Code:

```
/plugin marketplace add Guillaume-de-Maleprade/speak
/plugin install speak@speak
```

It's also listed in the [g2m marketplace](https://github.com/Guillaume-de-Maleprade/skills) as `speak@g2m`.

Then run `/speak:setup`. It installs `mcp` and `pocket-tts` into the `python` on PATH (the one the server starts with), downloads the voice model, and plays a test sentence. Expect 1-3 minutes: it downloads torch. To do it by hand instead:

```bat
python -m pip install "mcp>=2.3,<3" "pocket-tts>=4.0,<5"
```

Turn it on or off with `/plugin`: the MCP server starts and stops with it.

To test the local copy without installing it, from the repo root:

```bat
claude --plugin-dir .
```

Options, as environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SPEAK_LANG` | `fr` | Language when a call gives none |
| `SPEAK_PRELOAD` | `SPEAK_LANG` | Comma-separated languages to load at startup; empty to load nothing |
| `SPEAK_PREFIX` | git repo name | Said before every message, so you know which session talks; empty to say nothing |
| `SPEAK_DELAY_S` | `2` | Seconds between a call and the start of its playback, so the reply written after the call is on screen when the voice starts; `0` to play at once |

## Measured on a Windows laptop CPU (2026-11-12)

| Step | Time |
| --- | --- |
| MCP handshake | 0.7 s (torch is imported later, in the worker) |
| Import + load the first language | ~3.6 s, once per session |
| Load another language | ~1 s |
| Generate a 2–3 s sentence | 0.3–0.7 s (4–5x real-time) |

## Licenses

Code: MIT. Pocket TTS: MIT code, CC-BY 4.0 weights; each voice has its own license (see `kyutai/tts-voices` on Hugging Face).
