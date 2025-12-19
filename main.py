"""jarvis_basic.py

A basic, local Jarvis-style assistant.

Features (basic for now):
- Wake word (optional): "jarvis" (can be disabled)
- Speech-to-text (optional) using SpeechRecognition (Google Web Speech, free but requires internet)
- Text-to-speech using pyttsx3 (offline)
- Simple command router:
  - open websites
  - tell time/date
  - quick math
  - web search
  - take notes
  - run shell commands (opt-in allowlist)

How to run:
1) Install dependencies:
   pip install pyttsx3 SpeechRecognition pyaudio python-dateutil

   Notes:
   - On macOS, PyAudio install can be tricky. If pip fails:
     brew install portaudio
     pip install pyaudio
   - If you don't want voice input, you can run in text-only mode.

2) Run:
   python jarvis_basic.py

Security:
- This script includes an optional shell-command feature.
  It is disabled by default and only allows a small allowlist when enabled.

You can extend this later with:
- OpenAI/other LLM integration
- reminders, calendar, email
- custom skills/plugins
"""

from __future__ import annotations

import math
import os
import re
import shlex
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

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

TEXT_ONLY_MODE = False  # Set True to disable microphone and type commands.

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


class Listener:
    def __init__(self) -> None:
        self.recognizer = None
        self.microphone = None
        if sr is not None and not TEXT_ONLY_MODE:
            try:
                self.recognizer = sr.Recognizer()
                self.microphone = sr.Microphone()
            except Exception:
                self.recognizer = None
                self.microphone = None

    def listen(self) -> str:
        """Return recognized text (lowercased). Falls back to typed input."""
        if TEXT_ONLY_MODE or self.recognizer is None or self.microphone is None:
            return input("YOU: ").strip().lower()

        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
            audio = self.recognizer.listen(source, timeout=None, phrase_time_limit=8)

        try:
            text = self.recognizer.recognize_google(audio)
            return text.strip().lower()
        except sr.UnknownValueError:
            return ""
        except Exception:
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


def cmd_time(_: str) -> Response:
    now = datetime.now()
    return Response(spoken=f"It is {now.strftime('%-I:%M %p')}.")


def cmd_date(_: str) -> Response:
    now = datetime.now()
    return Response(spoken=f"Today is {now.strftime('%A, %B %-d, %Y')}.")


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
    url = "https://www.google.com/search?q=" + webbrowser.quote(query) if hasattr(webbrowser, "quote") else "https://www.google.com/search?q=" + query.replace(" ", "+")
    webbrowser.open(url)
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

    # Block obviously dangerous characters
    if any(tok in expr for tok in ["__", ";", "import", "os.", "sys.", "subprocess", "open("]):
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
            spoken = f"{int(val)}"
        else:
            spoken = f"{val}"
        return Response(spoken=spoken)
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
    # Read just a preview of the latest 5
    previews = []
    for fp in files[:5]:
        try:
            line = fp.read_text(encoding="utf-8").strip().splitlines()[0]
        except Exception:
            line = "(unreadable)"
        previews.append(f"- {fp.name}: {line[:80]}")

    return Response(spoken=f"Here are your latest notes.", detail="\n".join(previews))


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
        # Speak a short summary; show full output in detail
        short = out.splitlines()[0][:120]
        return Response(spoken=short, detail=out)
    except subprocess.CalledProcessError as e:
        return Response(spoken="That command failed.", detail=str(e.output))
    except Exception as e:
        return Response(spoken="I couldn't run that.", detail=str(e))


# Router map: (predicate -> handler)
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
    # Accept "jarvis ..." or "hey jarvis ..."
    t = re.sub(r"^hey\s+", "", t)
    if t.startswith(WAKE_WORD + " "):
        return t[len(WAKE_WORD) + 1 :].strip()
    if t == WAKE_WORD:
        return ""
    return t


def main() -> int:
    speaker = Speaker()
    listener = Listener()

    if pyttsx3 is None:
        print("[Info] pyttsx3 not installed; running without voice output.")
    if sr is None and not TEXT_ONLY_MODE:
        print("[Info] SpeechRecognition not installed; switching to text-only mode.")

    speaker.say("Online. Say 'help' for commands.")

    while True:
        raw = listener.listen()
        if not raw:
            continue

        text = strip_wake_word(raw)

        # If wake word is enabled and user didn't say it, ignore.
        if WAKE_WORD_ENABLED:
            lowered = raw.strip().lower()
            if not (lowered == WAKE_WORD or lowered.startswith(WAKE_WORD + " ") or lowered.startswith("hey " + WAKE_WORD)):
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


if __name__ == "__main__":
    raise SystemExit(main())
