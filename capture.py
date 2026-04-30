import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from kb_utils import insert_journal_bullet, today_journal

try:
    from rich.console import Console
    from rich.rule import Rule
    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

MODEL = "/usr/share/whisper.cpp-model-base.en-q5_1/ggml-base.en-q5_1.bin"
TMPWAV = "/tmp/lo-capture.wav"
TMPOUT = "/tmp/lo-capture-out"
PENDING_DIR = Path.home() / ".local" / "share" / "lo" / "pending-audio"


TERMUX_BIN = "/data/data/com.termux/files/usr/bin"
MIC_RECORD = f"{TERMUX_BIN}/termux-microphone-record"


def is_termux():
    return os.path.isdir(TERMUX_BIN)


def load_groq_key():
    key = os.environ.get("GROQ_API_KEY", "")
    if key:
        return key
    secrets = Path.home() / ".config/life-os/secrets.env"
    if secrets.exists():
        for line in secrets.read_text().splitlines():
            if line.startswith("GROQ_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def groq_transcribe(audio_path):
    """Returns (text, is_offline). is_offline=True means network unreachable."""
    api_key = load_groq_key()
    if not api_key:
        print("(GROQ_API_KEY not set — cannot transcribe)")
        return "", False
    result = subprocess.run(
        ["curl", "-s", "--max-time", "10",
         "https://api.groq.com/openai/v1/audio/transcriptions",
         "-H", f"Authorization: Bearer {api_key}",
         "-F", f"file=@{audio_path};type=audio/wav",
         "-F", "model=whisper-large-v3-turbo",
         "-F", "response_format=json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "", True  # network failure
    try:
        return json.loads(result.stdout).get("text", "").strip(), False
    except Exception:
        return "", False


def save_pending(wav_path):
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    dest = PENDING_DIR / f"{stamp}.wav"
    Path(wav_path).rename(dest)
    return dest


def kb_push(stamp):
    kb_dir = str(Path.home() / "kb")
    subprocess.run(["git", "-C", kb_dir, "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", kb_dir, "commit", "-m", f"capture {stamp}"],
                   capture_output=True)
    subprocess.run(["git", "-C", kb_dir, "pull", "--rebase", "origin", "main"],
                   capture_output=True)
    result = subprocess.run(["git", "-C", kb_dir, "push"],
                            capture_output=True, text=True)
    if result.returncode == 0:
        print("Synced.")
    else:
        print(f"Push failed: {result.stderr.strip() or 'unknown error'}")

inbox_path = Path.home() / "kb" / "inbox"


def unique_inbox_path(stamp, index=None):
    suffix = f"-{index}" if index is not None else ""
    path = inbox_path / f"{stamp}{suffix}.md"
    # If collision (same second, no index), add -1, -2...
    if path.exists() and index is None:
        n = 1
        while True:
            path = inbox_path / f"{stamp}-{n}.md"
            if not path.exists():
                break
            n += 1
    return path


capture_log = Path.home() / "kb" / "capture-log.md"


def log_capture(text, stamp):
    entry = f"**{stamp}**  {text}\n"
    existing = capture_log.read_text() if capture_log.exists() else ""
    capture_log.write_text(entry + existing)


def write_inbox(text, stamp, index=None):
    path = unique_inbox_path(stamp, index)
    path.write_text(text)
    log_capture(text, stamp)
    return path


def append_journal(text):
    insert_journal_bullet(text)


def text_mode(journal=False):
    inbox_path.mkdir(parents=True, exist_ok=True)
    count = 0

    console.print()
    dest_label = "journal" if journal else "inbox"
    console.print(Rule(f"[bold steel_blue1]  Capture → {dest_label}  [/bold steel_blue1]", style="steel_blue1 dim"))
    console.print()

    try:
        while True:
            try:
                line = input("  > ").strip()
            except EOFError:
                break

            if not line:
                break

            # Refresh stamp each line so rapid entries get unique names
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")

            if journal:
                append_journal(line)
                console.print("[dark_sea_green4]    → journal[/dark_sea_green4]")
            else:
                path = write_inbox(line, stamp)
                console.print(f"[dark_sea_green4]    → inbox/{path.name}[/dark_sea_green4]")

            count += 1

    except KeyboardInterrupt:
        pass

    console.print()
    if count:
        console.print(f"  [grey50]{count} item{'s' if count != 1 else ''} → {dest_label}[/grey50]")
    console.print()
    console.print(Rule(style="steel_blue1 dim"))
    console.print()


def voice_mode_termux(journal=False):
    """Voice capture for Termux: each round → separate file(s). 'break'/'brake' splits within a round."""
    inbox_path.mkdir(parents=True, exist_ok=True)
    tmpwav = str(Path.home() / "capture.wav")

    # Kill any stale recording
    subprocess.run([MIC_RECORD, "-q"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    total_written = 0
    round_num = 0

    print("\nVoice capture — Enter = next item, Ctrl+C when done\n")

    try:
        while True:
            round_num += 1
            print(f"[{round_num}] Recording...")

            Path(tmpwav).unlink(missing_ok=True)
            subprocess.run([MIC_RECORD, "-f", tmpwav, "-l", "0"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                input()
            except EOFError:
                pass

            subprocess.run([MIC_RECORD, "-q"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.5)

            if not Path(tmpwav).exists() or Path(tmpwav).stat().st_size == 0:
                print("(no audio — try again)\n")
                continue

            print("Transcribing...")
            text, offline = groq_transcribe(tmpwav)

            if offline:
                pending = save_pending(tmpwav)
                print(f"(offline — saved to pending: {pending.name})\n")
                continue

            Path(tmpwav).unlink(missing_ok=True)

            if not text:
                print("(transcription error — audio discarded)\n")
                continue

            # Split on "break" — each chunk becomes its own file
            chunks = re.split(r'\b(?:break|brake)\b[.,]?\s*', text, flags=re.IGNORECASE)
            chunks = [c.strip() for c in chunks if re.search(r'\w', c)]

            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
            use_index = len(chunks) > 1

            for i, chunk in enumerate(chunks, 1):
                if journal:
                    append_journal(chunk)
                    print(f"  → journal")
                else:
                    index = i if use_index else None
                    path = write_inbox(chunk, stamp, index)
                    print(f"  → inbox/{path.name}")
                display = chunk if len(chunk) <= 80 else chunk[:77] + "..."
                print(f"     {display}")
                total_written += 1

            print()

    except KeyboardInterrupt:
        subprocess.run([MIC_RECORD, "-q"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    if not total_written:
        print("\nNothing captured.")
        return

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    print(f"{total_written} item{'s' if total_written != 1 else ''} captured.")
    kb_push(stamp)


def flush_pending(journal=False):
    wavs = sorted(PENDING_DIR.glob("*.wav"))
    if not wavs:
        print("No pending recordings.")
        return

    print(f"{len(wavs)} pending recording(s).\n")
    written = 0

    for wav in wavs:
        print(f"Transcribing {wav.name}...")
        text, offline = groq_transcribe(str(wav))

        if offline:
            print("(still offline — stopping)")
            break

        if not text:
            print(f"(transcription error — keeping {wav.name})\n")
            continue

        chunks = re.split(r'\b(?:break|brake)\b[.,]?\s*', text, flags=re.IGNORECASE)
        chunks = [c.strip() for c in chunks if re.search(r'\w', c)]

        stamp = wav.stem  # preserve original timestamp
        use_index = len(chunks) > 1

        for i, chunk in enumerate(chunks, 1):
            if journal:
                append_journal(chunk)
                print(f"  → journal")
            else:
                index = i if use_index else None
                path = write_inbox(chunk, stamp, index)
                print(f"  → inbox/{path.name}")
            display = chunk if len(chunk) <= 80 else chunk[:77] + "..."
            print(f"     {display}")
            written += 1

        wav.unlink()
        print()

    if written:
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        print(f"{written} item(s) filed.")
        kb_push(stamp)


def voice_mode(journal=False):
    if is_termux():
        voice_mode_termux(journal=journal)
        return

    inbox_path.mkdir(parents=True, exist_ok=True)

    # Clean up any stale temp files
    for f in [TMPWAV, TMPOUT + ".txt"]:
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass

    console.print()
    console.print(Rule("[bold steel_blue1]  Capture → voice  [/bold steel_blue1]", style="steel_blue1 dim"))
    console.print()
    console.print("  [grey70]Recording...[/grey70]  [grey50]Ctrl+C when done[/grey50]")
    console.print()

    proc = subprocess.Popen(
        ["arecord", "-r", "16000", "-f", "S16_LE", "-c", "1", TMPWAV],
        stderr=subprocess.DEVNULL,
    )

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()

    console.print("\n  [grey50]Transcribing...[/grey50]")

    result = subprocess.run(
        ["whisper-cli", TMPWAV, "-m", MODEL, "-otxt", "-of", TMPOUT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        os.unlink(TMPWAV)
    except FileNotFoundError:
        pass

    txt_file = Path(TMPOUT + ".txt")
    if not txt_file.exists():
        console.print("  [rosy_brown](no transcription output)[/rosy_brown]")
        return

    text = txt_file.read_text().strip()
    txt_file.unlink()

    if not text:
        console.print("  [rosy_brown](no speech detected)[/rosy_brown]")
        return

    # Split on whole-word "break" or "brake" (case-insensitive), consuming trailing punctuation
    chunks = re.split(r'\b(?:break|brake)\b[.,]?\s*', text, flags=re.IGNORECASE)
    chunks = [c.strip() for c in chunks if c.strip()]

    if not chunks:
        console.print("  [rosy_brown](nothing captured)[/rosy_brown]")
        return

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    use_index = len(chunks) > 1
    console.print()

    for i, chunk in enumerate(chunks, 1):
        if journal:
            append_journal(chunk)
            console.print(f"[dark_sea_green4]  [{i}] → journal[/dark_sea_green4]")
        else:
            index = i if use_index else None
            path = write_inbox(chunk, stamp, index)
            console.print(f"[dark_sea_green4]  [{i}] → inbox/{path.name}[/dark_sea_green4]")

        display = chunk if len(chunk) <= 80 else chunk[:77] + "..."
        console.print(f"      [grey70]{display}[/grey70]")

    console.print()
    console.print(Rule(style="steel_blue1 dim"))
    console.print()


def main():
    parser = argparse.ArgumentParser(prog="lo capture", add_help=False)
    parser.add_argument("--voice", "-v", action="store_true")
    parser.add_argument("--journal", "-j", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args()

    if args.help:
        print("""
  lo capture            quick text capture — one line per item → inbox
  lo capture --voice    voice capture — say 'break'/'brake' between items, Ctrl+C to finish
                        (pending offline recordings are processed automatically on next use)
  lo capture --journal  capture directly to today's journal (with either mode)
""")
        return

    # Auto-drain pending offline recordings on any capture invocation (Termux only)
    if is_termux():
        pending = sorted(PENDING_DIR.glob("*.wav"))
        if pending:
            print(f"Processing {len(pending)} pending recording(s)...")
            flush_pending(journal=args.journal)
            print()

    if args.voice:
        voice_mode(journal=args.journal)
    else:
        text_mode(journal=args.journal)


if __name__ == "__main__":
    main()
