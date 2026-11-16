"""speak-mcp: an MCP server that lets a coding agent speak out loud, offline.

Text-to-speech runs in-process with Kyutai Pocket TTS on the CPU. Each language model
loads the first time it is used (about 1 s once the weights are cached), so the client
can choose the language on every call. One worker thread generates and plays the
calls in order, and a machine-wide named mutex serializes playback across processes,
so two calls never talk over each other, even from separate sessions.

Environment:
    SPEAK_LANG     default language when a call gives none (default: fr)
    SPEAK_PRELOAD  comma-separated languages to load at startup (default: SPEAK_LANG;
                   empty string to load nothing)
    SPEAK_PREFIX   said before every message (default: the name of the git repo the
                   server was started in, else its folder; empty string to say nothing)
    SPEAK_DELAY_S  seconds between a call and the start of its playback, so the reply
                   written right after the call is on screen when the voice starts
                   (default: 2; 0 to play as soon as the audio is ready)
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Literal, get_args

from mcp.server.mcpserver import MCPServer

log = logging.getLogger("speak_mcp")

Lang = Literal["fr", "en", "de", "es", "it", "pt", "nl"]
POCKET_LANGUAGE: dict[str, str] = {
    "fr": "french",
    "en": "english",
    "de": "german",
    "es": "spanish",
    "it": "italian",
    "pt": "portuguese",
    "nl": "dutch",
}
MAX_CHARS = 1000
WAIT_TIMEOUT_S = 120
LEAD_SILENCE_S = 0.5  # silence before each sentence, so the audio device is awake for the first word
PLAYBACK_MUTEX = "Local\\speak-mcp-playback"  # shared by every speak-mcp process of this user

DEFAULT_LANG: str = os.environ.get("SPEAK_LANG", "fr")
if DEFAULT_LANG not in POCKET_LANGUAGE:
    DEFAULT_LANG = "fr"

try:
    DELAY_S: float = max(0.0, float(os.environ.get("SPEAK_DELAY_S", "2")))
except ValueError:
    DELAY_S = 2.0


@dataclass
class Job:
    text: str  # empty text = load the language only
    lang: str
    submitted: float = field(default_factory=time.monotonic)
    done: threading.Event = field(default_factory=threading.Event)
    error: str | None = None


class Speaker:
    """Owns the models and the playback order. Only the worker thread touches the models."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[Job] = queue.Queue()
        self._voices: dict[str, tuple[object, object]] = {}  # lang -> (model, voice state)
        threading.Thread(target=self._run, name="speak-worker", daemon=True).start()

    def submit(self, text: str, lang: str) -> Job:
        job = Job(text=text, lang=lang)
        self._jobs.put(job)
        return job

    def _voice(self, lang: str) -> tuple[object, object]:
        if lang not in self._voices:
            # Imported here: torch takes ~3 s to import, and the MCP handshake should not wait.
            from pocket_tts import TTSModel
            from pocket_tts.default_parameters import get_default_voice_for_language

            language = POCKET_LANGUAGE[lang]
            log.info("loading Pocket TTS model: %s", language)
            model = TTSModel.load_model(language=language)
            state = model._cached_get_state_for_audio_prompt(get_default_voice_for_language(language))
            self._voices[lang] = (model, state)
        return self._voices[lang]

    def _run(self) -> None:
        import winsound

        playback = _PlaybackMutex(PLAYBACK_MUTEX)
        while True:
            job = self._jobs.get()
            try:
                model, state = self._voice(job.lang)
                if job.text:
                    # Generate outside the lock, so another session's sentence can play meanwhile.
                    wav = _to_wav(model.generate_audio(state, job.text), model.sample_rate)
                    # Generation time counts toward the delay: wait only for what is left.
                    remaining = job.submitted + DELAY_S - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
                    with playback:
                        winsound.PlaySound(wav, winsound.SND_MEMORY)
            except Exception as exc:  # report to the caller, keep the worker alive
                log.exception("speak failed")
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.done.set()


class _PlaybackMutex:
    """A Windows named mutex: at most one speak-mcp process plays audio at a time.

    A mutex belongs to the thread that acquires it, so only the worker thread uses this.
    If a process dies while holding it, Windows hands it to the next waiter (WAIT_ABANDONED).
    """

    _INFINITE = 0xFFFFFFFF
    _WAIT_FAILED = 0xFFFFFFFF

    def __init__(self, name: str) -> None:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        self._k32 = k32
        self._handle = k32.CreateMutexW(None, False, name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def __enter__(self) -> None:
        if self._k32.WaitForSingleObject(self._handle, self._INFINITE) == self._WAIT_FAILED:
            import ctypes

            raise ctypes.WinError(ctypes.get_last_error())

    def __exit__(self, *exc) -> None:
        self._k32.ReleaseMutex(self._handle)


def _project_name() -> str:
    """Name of the git repo in the working directory, said before each message.

    Uses the common git dir, so a worktree reads as its main repo, not as the
    worktree folder. Dashes and underscores become spaces so the voice reads words.
    """
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        name = os.path.basename(os.path.dirname(os.path.normpath(common)))
    except (OSError, subprocess.SubprocessError):
        name = os.path.basename(os.getcwd())
    return re.sub(r"[-_.]+", " ", name).strip()


def _to_wav(audio, sample_rate: int) -> bytes:
    """Mono float tensor in [-1, 1] -> 16-bit PCM WAV bytes, with a correct header.

    Starts with LEAD_SILENCE_S of silence: playback that starts cold clips the first word.
    """
    silence = b"\x00\x00" * int(sample_rate * LEAD_SILENCE_S)
    pcm = silence + (audio.detach().cpu().clamp(-1, 1).reshape(-1).numpy() * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


server = MCPServer(
    name="speak",
    instructions=(
        "Speak short updates out loud to the user, who may not be looking at the screen. "
        "Use it for progress during long tasks ('tests are running, about two minutes'), "
        "for questions you need answered before you continue, and for the result. "
        "Write for the ear: one or two short sentences, no code, paths, URLs or symbols. "
        "Describe results in terms of what the user sees or does, not the files. "
        "Only the main agent talking with the user speaks: a subagent or delegated task "
        "never calls this tool; it reports back to its parent, which speaks for it. "
        "Speech summarizes the written reply, it does not replace it: speak the result "
        "first, then write the full answer as your last message; playback starts a moment "
        "after the call, so the voice and the text arrive together. Never mention in "
        "text that something was queued or spoken."
    ),
)
speaker: Speaker | None = None
prefix: str = ""


@server.tool()
async def speak(text: str, lang: Lang | None = None, wait: bool = False) -> str:
    """Say a short sentence out loud on the user's speakers.

    Args:
        text: What to say: one or two short sentences, written for the ear.
        lang: Language of the text. Defaults to the server's configured language.
        wait: True to return only after the sentence has been spoken, for example
            before asking a question. False (default) returns at once and keeps working.
    """
    assert speaker is not None
    text = " ".join(text.split())
    if not text:
        return "Nothing to say: text is empty."
    lang = lang or DEFAULT_LANG
    if lang not in POCKET_LANGUAGE:
        return f"Unsupported language {lang!r}. Use one of: {', '.join(get_args(Lang))}."
    truncated = len(text) > MAX_CHARS
    said = f"{prefix}. {text[:MAX_CHARS]}" if prefix else text[:MAX_CHARS]
    job = speaker.submit(said, lang)
    note = f" (truncated to {MAX_CHARS} characters)" if truncated else ""
    if prefix:
        note += f", prefixed with {prefix!r}"
    if not wait:
        return f"Queued in {lang}{note}."
    finished = await asyncio.to_thread(job.done.wait, WAIT_TIMEOUT_S)
    if not finished:
        return f"Still speaking after {WAIT_TIMEOUT_S} s; giving up waiting."
    if job.error:
        return f"Could not speak: {job.error}"
    return f"Spoken in {lang}{note}."


def main() -> None:
    global speaker
    if sys.platform != "win32":
        sys.exit("speak-mcp plays audio with winsound, so it only runs on Windows for now.")
    # stdout carries the MCP protocol: every log line must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="speak-mcp: %(message)s")
    logging.getLogger("pocket_tts").setLevel(logging.WARNING)  # it logs every generation at INFO
    global prefix
    prefix = os.environ["SPEAK_PREFIX"].strip() if "SPEAK_PREFIX" in os.environ else _project_name()
    log.info("prefix: %r (cwd %s)", prefix, os.getcwd())
    speaker = Speaker()
    preload = os.environ.get("SPEAK_PRELOAD", DEFAULT_LANG)
    for lang in (p.strip() for p in preload.split(",")):
        if lang in POCKET_LANGUAGE:
            speaker.submit("", lang)
    server.run("stdio")


if __name__ == "__main__":
    main()
