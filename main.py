"""jarvis_basic.py

A basic, local Jarvis-style assistant.

Why you saw an error
- Some sandboxed environments have no interactive stdin. Calling input() can raise:
  OSError: [Errno 29] I/O error

This version fixes that by:
- Detecting non-interactive stdin and handling input() failures gracefully
- Providing a non-interactive mode via env var JARVIS_COMMANDS

Features (basic for now):
- Wake word (optional): "jarvis" (can be disabled)
- Speech-to-text (optional) using SpeechRecognition (Google Web Speech, requires internet)
- Text-to-speech using pyttsx3 (offline)
- Simple commands:
  - open websites
  - tell time/date
  - quick math
  - web search
  - take notes
  - run shell commands (opt-in allowlist)

How to run:
1) Install dependencies (voice output/input optional):
   pip install pyttsx3 SpeechRecognition pyaudio

   Notes:
   - On macOS, PyAudio install can be tricky. If pip fails:
       brew install portaudio
       pip install pyaudio
   - If you don't want voice input, set TEXT_ONLY_MODE=True.

2) Run interactively:
   python jarvis_basic.py

3) Run non-interactively (no stdin available):
   # Semicolon-separated commands. Wake word is optional depending on WAKE_WORD_ENABLED.
   JARVIS_COMMANDS="jarvis time; jarvis calculate 12*7; jarvis note buy milk; jarvis exit" python jarvis_basic.py

Security:
- Optional shell-command feature is disabled by default and uses an allowlist when enabled.
"""

from __future__ import annotations

import math
import re
import shlex
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import quote_plus

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

# Optional voice input
try:
    import speech_recognition as sr
except Exception:
    sr = None


# -----------------------------
# Configuration
# -----------------------------

WAKE_WORD_ENABLED = True
WAKE_WORD = "jarvis"

TEXT_ONLY_MODE = False  # Set True to disable microphone and (normally) type commands.

NOTES_DIR = Path.home() / ".jarvis_notes"
NOTES_DIR.mkdir(parents=True, exist_ok=True)

# Shell commands (dangerous if unrestricted). Disabled by default.
ENABLE_SHELL_COMMANDS = False
ALLOWED_SHELL_COMMANDS = {
    # examples (add what you need)
    "ls",
    "pwd",
    "whoami",
    "date",
}

# If stdin isn't interactive, we can still run scripted commands from this env var.
# Example: JARVIS_COMMANDS="jarvis time; jarvis exit"
JARVIS_COMMANDS_ENV = "JARVIS_COMMANDS"


# -----------------------------
# Utilities
# -----------------------------

@dataclass
class Response:
    spoken: str
    detail: Optional[str] = None


class Speaker:
    def __init__(self) -> None:
        self.engine = None
        if pyttsx3 is not None:
            try:
                self.engine = pyttsx3.init()
                # You can tweak voice/rate/volume here
                self.engine.setProperty("rate", 185)
            except Exception:
                self.engine = None

    def say(self, text: str) -> None:
        print(f"JARVIS: {text}")
        if self.engine is None:
            return
        try:
            self.engine.say(text)
            self.engine.runAndWait()
        except Exception:
            # If TTS fails, fall back to print-only
            pass


def _stdin_is_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


class Listener:
    """Gets user input from either microphone, stdin, or scripted env commands."""

    def __init__(self, scripted_commands: Optional[list[str]] = None) -> None:
        self.recognizer = None
        self.microphone = None
        self.scripted_commands = scripted_commands or []
        self._script_idx = 0

        if sr is not None and not TEXT_ONLY_MODE:
            try:
                self.recognizer = sr.Recognizer()
                self.microphone = sr.Microphone()
            except Exception:
                self.recognizer = None
                self.microphone = None

    def _next_scripted(self) -> str:
        if self._script_idx >= len(self.scripted_commands):
            return ""
        cmd = self.scripted_commands[self._script_idx]
        self._script_idx += 1
        return cmd.strip().lower()

    def listen(self) -> str:
        """Return recognized text (lowercased).

        Order:
        1) If scripted commands were provided, consume them first.
        2) If mic is available and not TEXT_ONLY_MODE, use speech recognition.
        3) Else try stdin (input). If stdin isn't available, return empty string.
        """

        # 1) Scripted commands
        scripted = self._next_scripted()
        if scripted:
            print(f"YOU: {scripted}")
            return scripted

        # 2) Microphone
        if not TEXT_ONLY_MODE and self.recognizer is not None and self.microphone is not None:
            with self.microphone as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio = self.recognizer.listen(source, timeout=None, phrase_time_limit=8)

            try:
                text = self.recognizer.recognize_google(audio)
                return text.strip().lower()
            except Exception:
                return ""

        # 3) stdin
        if not _stdin_is_interactive():
            # No stdin available; caller can decide what to do.
            return ""

        try:
            return input("YOU: ").strip().lower()
        except (OSError, EOFError):
            return ""


# -----------------------------
# Skills / Commands
# -----------------------------

CommandHandler = Callable[[str], Optional[Response]]


def cmd_help(_: str) -> Response:
    return Response(
        spoken=(
            "I can do basics: time, date, open a website, search the web, math, and notes. "
            "Try: 'open youtube', 'search how to install fabric', 'note buy milk', 'time', 'date', 'calculate 12 * 7'."
        )
    )


def _format_time(dt: datetime) -> str:
    # Cross-platform (Windows doesn't support %-I)
    return dt.strftime("%I:%M %p").lstrip("0")


def _format_date(dt: datetime) -> str:
    # Cross-platform (Windows doesn't support %-d)
    day = str(dt.day)
    return dt.strftime(f"%A, %B {day}, %Y")


def cmd_time(_: str) -> Response:
    now = datetime.now()
    return Response(spoken=f"It is {_format_time(now)}.")


def cmd_date(_: str) -> Response:
    now = datetime.now()
    return Response(spoken=f"Today is {_format_date(now)}.")


def cmd_open(text: str) -> Optional[Response]:
    # Examples:
    # - open youtube
    # - open https://example.com
    m = re.match(r"^(open|go to)\s+(.+)$", text)
    if not m:
        return None

    target = m.group(2).strip()

    shortcuts = {
        "youtube": "https://www.youtube.com",
        "google": "https://www.google.com",
        "gmail": "https://mail.google.com",
        "github": "https://github.com",
        "reddit": "https://www.reddit.com",
    }

    url = shortcuts.get(target, target)
    if not re.match(r"^https?://", url):
        # Assume it's a domain
        url = "https://" + url

    try:
        webbrowser.open(url)
        return Response(spoken=f"Opening {target}.")
    except Exception:
        return Response(spoken="I couldn't open that.")


def cmd_search(text: str) -> Optional[Response]:
    # - search cats
    # - search for cats
    m = re.match(r"^search(\s+for)?\s+(.+)$", text)
    if not m:
        return None
    query = m.group(2).strip()
    url = "https://www.google.com/search?q=" + quote_plus(query)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return Response(spoken=f"Searching for {query}.")


def safe_eval_math(expr: str) -> float:
    """Safely evaluate basic math expressions.

    Allowed:
    - numbers
    - + - * / ** ( )
    - math functions/constants: sin, cos, tan, sqrt, pi, e, etc.

    Disallowed:
    - names not in allowlist
    - attribute access
    - import, etc.
    """
    expr = expr.strip()
    if not expr:
        raise ValueError("Empty expression")

    # Block obviously dangerous tokens
    if any(tok in expr for tok in ["__", ";", "import", "os.", "sys.", "subprocess", "open(", "eval", "exec"]):
        raise ValueError("Unsafe expression")

    allowed_names = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    allowed_names.update({"abs": abs, "round": round})

    code = compile(expr, "<math>", "eval")
    for name in code.co_names:
        if name not in allowed_names:
            raise ValueError(f"Name '{name}' is not allowed")

    return float(eval(code, {"__builtins__": {}}, allowed_names))


def cmd_calculate(text: str) -> Optional[Response]:
    # - calculate 2+2
    # - what's 2+2
    m = re.match(r"^(calculate|what's|whats|what is)\s+(.+)$", text)
    if not m:
        return None
    expr = m.group(2)
    try:
        val = safe_eval_math(expr)
        if val.is_integer():
            return Response(spoken=f"{int(val)}")
        return Response(spoken=f"{val}")
    except Exception:
        return Response(spoken="I couldn't calculate that.")


def cmd_note(text: str) -> Optional[Response]:
    # - note buy milk
    # - take a note buy milk
    m = re.match(r"^(note|take a note|save note)\s+(.+)$", text)
    if not m:
        return None
    content = m.group(2).strip()

    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    fp = NOTES_DIR / f"note_{ts}.txt"
    fp.write_text(content + "\n", encoding="utf-8")

    return Response(spoken="Saved.", detail=str(fp))


def cmd_list_notes(_: str) -> Response:
    files = sorted(NOTES_DIR.glob("note_*.txt"), reverse=True)
    if not files:
        return Response(spoken="You have no notes yet.")

    previews: list[str] = []
    for fp in files[:5]:
        try:
            line = fp.read_text(encoding="utf-8").strip().splitlines()[0]
        except Exception:
            line = "(unreadable)"
        previews.append(f"- {fp.name}: {line[:80]}")

    return Response(spoken="Here are your latest notes.", detail="\n".join(previews))


def cmd_shell(text: str) -> Optional[Response]:
    if not ENABLE_SHELL_COMMANDS:
        return None

    m = re.match(r"^(run|execute)\s+(.+)$", text)
    if not m:
        return None

    raw = m.group(2).strip()
    parts = shlex.split(raw)
    if not parts:
        return Response(spoken="No command provided.")

    exe = parts[0]
    if exe not in ALLOWED_SHELL_COMMANDS:
        return Response(spoken="That command isn't allowed.")

    try:
        out = subprocess.check_output(parts, stderr=subprocess.STDOUT, text=True)
        out = out.strip()
        if not out:
            return Response(spoken="Done.")
        short = out.splitlines()[0][:120]
        return Response(spoken=short, detail=out)
    except subprocess.CalledProcessError as e:
        return Response(spoken="That command failed.", detail=str(e.output))
    except Exception as e:
        return Response(spoken="I couldn't run that.", detail=str(e))


ROUTES: Tuple[CommandHandler, ...] = (
    cmd_open,
    cmd_search,
    cmd_calculate,
    cmd_note,
    cmd_shell,
)


def route(text: str) -> Response:
    if text in {"help", "commands", "what can you do"}:
        return cmd_help(text)
    if text in {"time", "what time is it"}:
        return cmd_time(text)
    if text in {"date", "what's the date", "whats the date"}:
        return cmd_date(text)
    if text in {"list notes", "show notes", "notes"}:
        return cmd_list_notes(text)
    if text in {"exit", "quit", "goodbye"}:
        return Response(spoken="Goodbye.")

    for handler in ROUTES:
        r = handler(text)
        if r is not None:
            return r

    return Response(spoken="I didn't understand. Say 'help' for commands.")


# -----------------------------
# Main loop
# -----------------------------


def strip_wake_word(text: str) -> str:
    if not WAKE_WORD_ENABLED:
        return text
    t = text.strip().lower()
    t = re.sub(r"^hey\s+", "", t)
    if t.startswith(WAKE_WORD + " "):
        return t[len(WAKE_WORD) + 1 :].strip()
    if t == WAKE_WORD:
        return ""
    return t


def _load_scripted_commands_from_env() -> list[str]:
    raw = (sys.environ.get(JARVIS_COMMANDS_ENV) if hasattr(sys, "environ") else None)  # type: ignore[attr-defined]
    if raw is None:
        # Fallback: os.environ is always available, but keep this safe.
        try:
            import os

            raw = os.environ.get(JARVIS_COMMANDS_ENV)
        except Exception:
            raw = None

    if not raw:
        return []

    # Split on semicolons; ignore empties
    return [c.strip() for c in raw.split(";") if c.strip()]


def main() -> int:
    speaker = Speaker()
    scripted = _load_scripted_commands_from_env()
    listener = Listener(scripted_commands=scripted)

    if pyttsx3 is None:
        print("[Info] pyttsx3 not installed; running without voice output.")

    if sr is None and not TEXT_ONLY_MODE:
        print("[Info] SpeechRecognition not installed; voice input unavailable.")

    # If we have no scripted commands and no interactive stdin and no mic, exit cleanly.
    if not scripted and TEXT_ONLY_MODE and not _stdin_is_interactive():
        speaker.say(
            "No interactive input is available. Set JARVIS_COMMANDS to run scripted commands, "
            "or run this in a normal terminal."
        )
        return 2

    speaker.say("Online. Say 'help' for commands.")

    while True:
        raw = listener.listen()

        # If nothing was captured and we were in scripted mode, end.
        if not raw:
            if scripted and listener._script_idx >= len(scripted):
                break
            continue

        text = strip_wake_word(raw)

        # If wake word is enabled and user didn't say it, ignore.
        if WAKE_WORD_ENABLED:
            lowered = raw.strip().lower()
            if not (
                lowered == WAKE_WORD
                or lowered.startswith(WAKE_WORD + " ")
                or lowered.startswith("hey " + WAKE_WORD)
            ):
                continue

        if not text:
            speaker.say("Yes?")
            continue

        resp = route(text)
        speaker.say(resp.spoken)
        if resp.detail:
            print(resp.detail)

        if text in {"exit", "quit", "goodbye"}:
            break

    return 0


# -----------------------------
# Tests
# -----------------------------


def _run_tests() -> int:
    import unittest

    class TestMath(unittest.TestCase):
        def test_basic(self) -> None:
            self.assertEqual(safe_eval_math("2+2"), 4.0)

        def test_functions(self) -> None:
            self.assertAlmostEqual(safe_eval_math("sqrt(9)"), 3.0)
            self.assertAlmostEqual(safe_eval_math("sin(pi/2)"), 1.0, places=7)

        def test_blocks_names(self) -> None:
            with self.assertRaises(ValueError):
                safe_eval_math("os.system('rm -rf /')")

        def test_blocks_dunders(self) -> None:
            with self.assertRaises(ValueError):
                safe_eval_math("__import__('os').system('echo hi')")

    class TestWakeWord(unittest.TestCase):
        def test_strip(self) -> None:
            global WAKE_WORD_ENABLED
            WAKE_WORD_ENABLED = True
            self.assertEqual(strip_wake_word("jarvis time"), "time")
            self.assertEqual(strip_wake_word("hey jarvis time"), "time")
            self.assertEqual(strip_wake_word("jarvis"), "")

    class TestRouting(unittest.TestCase):
        def test_route_help(self) -> None:
            r = route("help")
            self.assertIn("basics", r.spoken)

        def test_calculate(self) -> None:
            r = route("calculate 12*7")
            self.assertEqual(r.spoken, "84")

    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    # Some runners don't like loadTestsFromModule inside a function; build explicitly.
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestMath))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestWakeWord))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestRouting))

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if "--test" in sys.argv:
        raise SystemExit(_run_tests())
    raise SystemExit(main())
