"""jarvis_basic.py

Basic Jarvis-style assistant (voice optional).

Changes requested:
- TTS reliability on Replit: pyttsx3 often cannot output audio in hosted sandboxes.
  This version:
  - tries pyttsx3
  - if it fails, falls back to a "print-only" mode (no crash)
  - optional: gTTS-based audio file generation (may or may not play in Replit)

- Greetings:
  If you say "hello" or "hello how are you" (or any phrase starting with "hello"),
  it responds: "Hello sir, how can I help you today?"

- Notes:
  - One notes file per day (YYYY-MM-DD.txt)
  - Each note is appended with a timestamp
  - When you save a note, it confirms by repeating the note text (no filename)

Non-interactive environments:
- If stdin is not interactive, you can supply commands via env var JARVIS_COMMANDS:
  JARVIS_COMMANDS="jarvis hello; jarvis note buy milk; jarvis notes; jarvis exit" python jarvis_basic.py

Run tests:
  python jarvis_basic.py --test
"""

from __future__ import annotations

import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import quote_plus

# -----------------------------
# Optional dependencies
# -----------------------------

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

# Optional voice input
try:
    import speech_recognition as sr
except Exception:
    sr = None

# Optional TTS fallback (creates mp3 files)
try:
    from gtts import gTTS  # type: ignore
except Exception:
    gTTS = None


# -----------------------------
# Configuration
# -----------------------------

WAKE_WORD_ENABLED = True
WAKE_WORD = "jarvis"

TEXT_ONLY_MODE = False  # Set True to disable microphone.

# Notes are stored as one file per day.
NOTES_DIR = Path.home() / ".jarvis_notes"
NOTES_DIR.mkdir(parents=True, exist_ok=True)

# Shell commands (dangerous if unrestricted). Disabled by default.
ENABLE_SHELL_COMMANDS = False
ALLOWED_SHELL_COMMANDS = {"ls", "pwd", "whoami", "date"}

# Non-interactive scripted commands, separated by semicolons.
JARVIS_COMMANDS_ENV = "JARVIS_COMMANDS"

# TTS mode:
# - "auto": try pyttsx3, else print-only
# - "pyttsx3": force pyttsx3
# - "gtts": create an mp3 file using gTTS and attempt to open it
# - "print": print-only
TTS_MODE = os.environ.get("JARVIS_TTS", "auto").strip().lower()


# -----------------------------
# Utilities
# -----------------------------

@dataclass
class Response:
    spoken: str
    detail: Optional[str] = None


def _stdin_is_interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def _format_time(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _format_date(dt: datetime) -> str:
    day = str(dt.day)
    return dt.strftime(f"%A, %B {day}, %Y")


# -----------------------------
# I/O: Speaker & Listener
# -----------------------------

class Speaker:
    def __init__(self) -> None:
        self.mode = TTS_MODE
        self.engine = None
        self.pyttsx3_ok = False

        if self.mode in {"print", "gtts"}:
            return

        if pyttsx3 is None:
            if self.mode == "pyttsx3":
                self.mode = "print"
            return

        try:
            self.engine = pyttsx3.init()
            self.engine.setProperty("rate", 185)
            # Quick self-test (some environments initialize but fail on run)
            self.engine.say("")
            self.engine.runAndWait()
            self.pyttsx3_ok = True
        except Exception:
            self.engine = None
            self.pyttsx3_ok = False
            if self.mode in {"auto", "pyttsx3"}:
                # In Replit/sandboxes this often fails; fall back cleanly.
                self.mode = "print"

    def _say_with_pyttsx3(self, text: str) -> None:
        if not self.engine or not self.pyttsx3_ok:
            return
        try:
            self.engine.say(text)
            self.engine.runAndWait()
        except Exception:
            # If it fails mid-run, permanently fall back.
            self.pyttsx3_ok = False
            self.mode = "print"

    def _say_with_gtts(self, text: str) -> None:
        if gTTS is None:
            # Can't use gTTS; just print.
            return
        try:
            # Write to a temp mp3 file and try to open it.
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
                fp = f.name
            gTTS(text=text, lang="en").save(fp)
            # Attempt to open default player (may not play in some sandboxes).
            try:
                webbrowser.open(f"file://{fp}")
            except Exception:
                pass
            print(f"[Audio file created] {fp}")
        except Exception:
            pass

    def say(self, text: str) -> None:
        print(f"JARVIS: {text}")

        if self.mode == "print":
            return
        if self.mode == "gtts":
            self._say_with_gtts(text)
            return

        # auto/pyttsx3
        self._say_with_pyttsx3(text)


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
            return ""

        try:
            return input("YOU: ").strip().lower()
        except (OSError, EOFError):
            return ""


# -----------------------------
# Skills / Commands
# -----------------------------

CommandHandler = Callable[[str], Optional[Response]]


def cmd_greeting(text: str) -> Optional[Response]:
    # Match: "hello", "hello how are you", "hello jarvis" etc.
    if not text.startswith("hello"):
        return None
    return Response(spoken="Hello sir, how can I help you today?")


def cmd_help(_: str) -> Response:
    return Response(
        spoken=(
            "Commands: hello, time, date, open <site>, search <query>, calculate <expr>, "
            "note <text>, notes, exit."
        )
    )


def cmd_time(_: str) -> Response:
    now = datetime.now()
    return Response(spoken=f"It is {_format_time(now)}.")


def cmd_date(_: str) -> Response:
    now = datetime.now()
    return Response(spoken=f"Today is {_format_date(now)}.")


def cmd_open(text: str) -> Optional[Response]:
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
        url = "https://" + url

    try:
        webbrowser.open(url)
        return Response(spoken=f"Opening {target}.")
    except Exception:
        return Response(spoken="I couldn't open that.")


def cmd_search(text: str) -> Optional[Response]:
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
    expr = expr.strip()
    if not expr:
        raise ValueError("Empty expression")

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


def _notes_file_for_today(notes_dir: Path = NOTES_DIR, now: Optional[datetime] = None) -> Path:
    now = now or datetime.now()
    day = now.strftime("%Y-%m-%d")
    return notes_dir / f"{day}.txt"


def cmd_note(text: str) -> Optional[Response]:
    m = re.match(r"^(note|take a note|save note)\s+(.+)$", text)
    if not m:
        return None
    content = m.group(2).strip()

    fp = _notes_file_for_today()
    ts = datetime.now().strftime("%H:%M")
    with fp.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {content}\n")

    return Response(spoken=f"Saved: {content}")


def cmd_show_notes(_: str) -> Response:
    fp = _notes_file_for_today()
    if not fp.exists():
        return Response(spoken="You have no notes for today.")
    try:
        txt = fp.read_text(encoding="utf-8").strip()
    except Exception:
        return Response(spoken="I couldn't read today's notes.")

    if not txt:
        return Response(spoken="You have no notes for today.")

    # Speak a short summary; show full text in detail
    lines = txt.splitlines()
    summary = f"You have {len(lines)} note{'s' if len(lines) != 1 else ''} for today."
    return Response(spoken=summary, detail=txt)


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
        out = subprocess.check_output(parts, stderr=subprocess.STDOUT, text=True).strip()
        if not out:
            return Response(spoken="Done.")
        short = out.splitlines()[0][:120]
        return Response(spoken=short, detail=out)
    except subprocess.CalledProcessError as e:
        return Response(spoken="That command failed.", detail=str(e.output))
    except Exception as e:
        return Response(spoken="I couldn't run that.", detail=str(e))


ROUTES: Tuple[CommandHandler, ...] = (
    cmd_greeting,
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
    if text in {"notes", "show notes", "list notes", "today's notes", "todays notes"}:
        return cmd_show_notes(text)
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


def strip_wake_word(raw: str) -> tuple[bool, str]:
    """Return (has_wake_word, remaining_text)."""
    t = raw.strip().lower()
    if not WAKE_WORD_ENABLED:
        return True, t

    t2 = re.sub(r"^hey\s+", "", t)
    if t2 == WAKE_WORD:
        return True, ""
    if t2.startswith(WAKE_WORD + " "):
        return True, t2[len(WAKE_WORD) + 1 :].strip()
    return False, t


def _load_scripted_commands_from_env() -> list[str]:
    raw = os.environ.get(JARVIS_COMMANDS_ENV, "")
    if not raw.strip():
        return []
    return [c.strip() for c in raw.split(";") if c.strip()]


def main() -> int:
    speaker = Speaker()
    scripted = _load_scripted_commands_from_env()
    listener = Listener(scripted_commands=scripted)

    # If we have no scripted commands and no interactive stdin and no mic, exit cleanly.
    if not scripted and TEXT_ONLY_MODE and not _stdin_is_interactive():
        speaker.say(
            "No interactive input is available. Set JARVIS_COMMANDS to run scripted commands, "
            "or run this in a normal terminal."
        )
        return 2

    if speaker.mode == "print" and TTS_MODE != "print":
        print("[Info] TTS is unavailable in this environment; using print-only responses.")

    speaker.say("Online. Say 'help' for commands.")

    while True:
        raw = listener.listen()

        # If nothing was captured and we were in scripted mode, end.
        if not raw:
            if scripted and listener._script_idx >= len(scripted):
                break
            continue

        has_wake, text = strip_wake_word(raw)

        if WAKE_WORD_ENABLED and not has_wake:
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

        def test_blocks_dunders(self) -> None:
            with self.assertRaises(ValueError):
                safe_eval_math("__import__('os').system('echo hi')")

    class TestGreeting(unittest.TestCase):
        def test_hello(self) -> None:
            r = route("hello")
            self.assertEqual(r.spoken, "Hello sir, how can I help you today?")

        def test_hello_how_are_you(self) -> None:
            r = route("hello how are you")
            self.assertEqual(r.spoken, "Hello sir, how can I help you today?")

    class TestWakeWord(unittest.TestCase):
        def test_strip(self) -> None:
            global WAKE_WORD_ENABLED
            WAKE_WORD_ENABLED = True
            self.assertEqual(strip_wake_word("jarvis time"), (True, "time"))
            self.assertEqual(strip_wake_word("hey jarvis time"), (True, "time"))
            self.assertEqual(strip_wake_word("jarvis"), (True, ""))
            self.assertEqual(strip_wake_word("time"), (False, "time"))

    class TestNotesFilePerDay(unittest.TestCase):
        def test_notes_path(self) -> None:
            with tempfile.TemporaryDirectory() as td:
                d = Path(td)
                dt = datetime(2025, 12, 18, 10, 30)
                fp = _notes_file_for_today(notes_dir=d, now=dt)
                self.assertEqual(fp.name, "2025-12-18.txt")

    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestMath))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestGreeting))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestWakeWord))
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestNotesFilePerDay))

    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    if "--test" in sys.argv:
        raise SystemExit(_run_tests())
    raise SystemExit(main())
