import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from paths import KB, STORE

from kb_utils import insert_journal_bullet, today_journal

try:
    from rich.console import Console
    from rich.rule import Rule
    import theme
    # highlight=False: rich's default highlighter picks numbers out of any
    # string and bolds them, so a filename came out as a ransom note —
    # `2026`-`09`-`16` each in bold against the mute grey it was asked for.
    console = Console(highlight=False)
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Importing readline is what makes input() an editable line: arrow keys, ^A/^E
# and history instead of literal escape characters. It was never imported here,
# so `cl capture` has always been a dumb line reader.
#
# \001/\002 wrap the colour escapes. They tell readline "these bytes take no
# columns" — without them it thinks the prompt is ~13 characters wide and
# redraws long lines in the wrong place.
try:
    import readline  # noqa: F401
except ImportError:
    pass


def _prompt(hex_colour):
    esc = hex_colour.lstrip("#")
    r, g, b = (int(esc[i:i + 2], 16) for i in (0, 2, 4))
    return f"  \001\033[38;2;{r};{g};{b}m\002❯\001\033[0m\002 "

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
    """Sync after a capture. `stamp` is unused now: the one syncer writes its
    own commit message, and a per-capture message is not worth a second
    implementation of stage/commit/pull/push that can fail in silence."""
    from kb_utils import sync_kb
    ok, msg = sync_kb()
    print(msg if ok else f"kb sync FAILED: {msg}")

# There is no inbox FOLDER — "inbox" is the set of stream notes with no tags.
# This path said _stream/inbox until 2026-09-03, which meant every $mod+c
# capture recreated a retired directory and landed outside the month shards.
stream_path = STORE


def _shard():
    """The store is ONE FLAT DIRECTORY since the 2026-09-07 flatten.

    Kept as a function rather than inlined: every caller asks "where does a new
    capture go" and there must stay exactly one answer to that. paths.STORE was
    repointed to ~/kb/notes by the flatten, but this still appended YYYY/MM
    underneath it — so `cl capture`, and therefore Surface's POST /capture and
    the phone quick-capture behind it, rebuilt a shard tree inside the flat
    store. mkdir(parents=True) never errors, so nothing reported it."""
    stream_path.mkdir(parents=True, exist_ok=True)
    return stream_path


def unique_inbox_path(stamp, index=None):
    suffix = f"-{index}" if index is not None else ""
    path = _shard() / f"{stamp}{suffix}.md"
    # If collision (same second, no index), add -1, -2...
    if path.exists() and index is None:
        n = 1
        while True:
            path = _shard() / f"{stamp}-{n}.md"
            if not path.exists():
                break
            n += 1
    return path


# No capture log. Every capture used to be written TWICE -- once as a note,
# once prepended to ~/kb/capture-log.md -- and the second copy answered only
# "did my capture land?", which the inbox view now answers from the store
# itself. It also survived the flatten wrong: STORE was repointed to
# ~/kb/notes and this still said KB, so it silently abandoned its own 110-entry
# history in notes/ and started a fresh file OUTSIDE the store. Same bug as
# _shard() above, two lines down from its comment. A second writer to the
# capture path is the exact class of thing the flatten removed.


def write_inbox(text, stamp, index=None):
    """One capture, one file in working/ (2026-10-05). Tags are retired, so the
    frontmatter is just `created:` — the folder says it is waiting on a decision."""
    from datetime import datetime as _dt
    path = unique_inbox_path(stamp, index)
    path.write_text(f"---\ncreated: {_dt.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"---\n\n{text.strip()}\n")
    return path


def append_journal(text):
    insert_journal_bullet(text)


def text_mode(journal=False):
    stream_path.mkdir(parents=True, exist_ok=True)
    count = 0

    console.print()
    dest_label = "journal" if journal else "inbox"
    # The Surface capture lens, at terminal scale: the accent chip, the note's
    # destination in mute beside it, then a hairline in the border colour. No
    # box drawing — Surface has no frame around the composer either.
    console.print(f"  {theme.CHIP('CAPTURE')}  [{theme.MUTE}]→ {dest_label}[/]")
    console.print(Rule(style=theme.RULE))
    console.print()

    try:
        while True:
            try:
                # An accent caret, the way Surface sets caret-color:var(--accent)
                # on the composer. The prompt goes to input() itself, not to a
                # separate print — readline has to own the whole line.
                line = input(_prompt(theme.PROMPT)).strip()
            except EOFError:
                break

            if not line:
                break

            # Refresh stamp each line so rapid entries get unique names
            stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")

            # Surface flashes a green "captured" chip and the note appears in
            # the stream. Here the line you typed is already on screen, so the
            # confirmation is one quiet line under it: the tick in --done, the
            # filename in --mute, nothing competing with what you wrote.
            if journal:
                append_journal(line)
                console.print(f"    [{theme.DONE}]✓[/] [{theme.MUTE}]journal[/]")
            else:
                path = write_inbox(line, stamp)
                console.print(f"    [{theme.DONE}]✓[/] [{theme.MUTE}]{path.name}[/]")

            count += 1

    except KeyboardInterrupt:
        pass

    console.print()
    console.print(Rule(style=theme.RULE))
    if count:
        console.print(f"  [{theme.DIM}]{count} item{'s' if count != 1 else ''}[/] "
                      f"[{theme.MUTE}]→ {dest_label}[/]")
    else:
        console.print(f"  [{theme.MUTE}]nothing captured[/]")
    console.print()


def voice_mode_termux(journal=False):
    """Voice capture for Termux: each round → separate file(s). 'break'/'brake' splits within a round."""
    stream_path.mkdir(parents=True, exist_ok=True)
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
                    print(f"  → notes/{path.name}")
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
                print(f"  → notes/{path.name}")
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

    stream_path.mkdir(parents=True, exist_ok=True)

    # Clean up any stale temp files
    for f in [TMPWAV, TMPOUT + ".txt"]:
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass

    console.print()
    console.print(f"  {theme.CHIP('CAPTURE')}  [{theme.MUTE}]→ voice[/]")
    console.print(Rule(style=theme.RULE))
    console.print()
    console.print(f"  [{theme.ACCENT}]●[/] [{theme.INK}]Recording[/]  "
                  f"[{theme.MUTE}]Ctrl+C when done[/]")
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

    console.print(f"\n  [{theme.MUTE}]Transcribing…[/]")

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
        console.print(f"  [{theme.DIM}]no transcription output[/]")
        return

    text = txt_file.read_text().strip()
    txt_file.unlink()

    if not text:
        console.print(f"  [{theme.DIM}]no speech detected[/]")
        return

    # Split on whole-word "break" or "brake" (case-insensitive), consuming trailing punctuation
    chunks = re.split(r'\b(?:break|brake)\b[.,]?\s*', text, flags=re.IGNORECASE)
    chunks = [c.strip() for c in chunks if c.strip()]

    if not chunks:
        console.print(f"  [{theme.MUTE}]nothing captured[/]")
        return

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    use_index = len(chunks) > 1
    console.print()

    for i, chunk in enumerate(chunks, 1):
        if journal:
            append_journal(chunk)
            console.print(f"  [{theme.DONE}]✓[/] [{theme.MUTE}]journal[/]")
        else:
            index = i if use_index else None
            path = write_inbox(chunk, stamp, index)
            console.print(f"  [{theme.DONE}]✓[/] [{theme.MUTE}]{path.name}[/]")

        display = chunk if len(chunk) <= 80 else chunk[:77] + "…"
        console.print(f"    [{theme.INK}]{display}[/]")

    console.print()
    console.print(Rule(style=theme.RULE))
    console.print(f"  [{theme.DIM}]{len(chunks)} item{'s' if len(chunks) != 1 else ''}[/] "
                  f"[{theme.MUTE}]→ {'journal' if journal else 'inbox'}[/]")
    console.print()


def main():
    parser = argparse.ArgumentParser(prog="lo capture", add_help=False)
    parser.add_argument("--voice", "-v", action="store_true")
    parser.add_argument("--journal", "-j", action="store_true")
    parser.add_argument("--text", default=None,
                        help="non-interactive: capture this string and exit (web/automation)")
    parser.add_argument("-h", "--help", action="store_true")
    args = parser.parse_args()

    if args.help:
        print("""
  lo capture            quick text capture — one line per item → inbox
  lo capture --voice    voice capture — say 'break'/'brake' between items, Ctrl+C to finish
                        (pending offline recordings are processed automatically on next use)
  lo capture --journal  RETIRED 2026-10-05 (no daily notes)
  lo capture --text STR non-interactive: capture STR and exit (web/automation)
""")
        return

    if args.journal:
        # RETIRED 2026-10-05: no daily notes any more, and writing/journal/ is
        # Miro's own hand — nothing appends to it automatically.
        print("cl capture --journal is retired (2026-10-05): no daily notes. "
              "Plain `cl capture` lands in working/.")
        return

    if args.text is not None:
        text = args.text.strip()
        if not text:
            print("(empty — nothing captured)")
            return
        stream_path.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        if args.journal:
            append_journal(text)
            print("→ journal")
        else:
            path = write_inbox(text, stamp)
            print(f"→ working/{path.name}")
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
