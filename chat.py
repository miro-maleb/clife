"""chat.py — `cl chat` : an interactive project-fleshing conversation with local qwen.

The point of this tool is *seamless* switching between fast (nothink) and deep
(think) responses inside one conversation, without losing project context:

  - Default is FAST (nothink) — snappy back-and-forth.
  - `.t` toggles a sticky THINK mode on/off mid-stream.
  - Prefix ONE message with `?` to think for just that turn, then revert.

The model, conversation history, and the loaded project note all persist across
the switch — flipping think on/off never resets the thread.

Model: local qwen via ollama. Defaults to qwen3.6:27b (the 27B is a real step up
over the 8B for open-ended project thinking — it catches the non-obvious). Fast
turns on the 27B run ~25-35s; `.m qwen3:8b` drops that to a few seconds if you
want pure speed over depth. Streams tokens so it feels live regardless.
"""
import datetime
import json
import os
import sys
import urllib.request

try:
    import readline  # noqa: F401 — line editing + history in input()
except ImportError:
    pass

from paths import KB, STORE

OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = os.environ.get("CL_CHAT_MODEL", "qwen3.6:27b")
PROJECTS = STORE

# ANSI — clife already leans on rich elsewhere, but raw codes keep streaming simple.
DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RST = "\033[0m"

SYSTEM = (
    "You are a sharp, concrete thinking partner helping flesh out a personal "
    "project note in a markdown knowledge base. Be specific and actionable. No "
    "filler, no restating the obvious. Propose real categories, targets, next "
    "steps, or structure the person can actually use. When you make an "
    "assumption, state it. Match the note's own goals and orientations."
)


# ---------------------------------------------------------------- projects ---
def find_project(slug):
    """Locate a project.md by its folder name (unique across the tree)."""
    for md in PROJECTS.rglob("project.md"):
        if md.parent.name == slug:
            return md
    return None


def all_projects():
    """[(slug, area, title, path)] for every project.md, sorted by slug."""
    out = []
    for md in sorted(PROJECTS.rglob("project.md")):
        title = md.parent.name.replace("-", " ").title()
        for line in md.read_text().splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        try:
            area = md.relative_to(PROJECTS).parts[0]
        except ValueError:
            area = "?"
        out.append((md.parent.name, area, title, md))
    return out


def pick_project():
    """fzf over projects; numbered-menu fallback if fzf is absent."""
    projects = all_projects()
    if not projects:
        print("no projects found under", PROJECTS)
        sys.exit(1)
    import shutil
    import subprocess

    lines = [f"{slug}\t{area}\t{title}" for slug, area, title, _ in projects]
    if shutil.which("fzf"):
        try:
            r = subprocess.run(
                ["fzf", "--with-nth=1,3", "--delimiter=\t",
                 "--prompt=project> ", "--height=40%", "--reverse"],
                input="\n".join(lines), capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                slug = r.stdout.split("\t", 1)[0].strip()
                return find_project(slug)
        except Exception:
            pass
        return None
    for i, (slug, area, title, _) in enumerate(projects, 1):
        print(f"  {i:2}. {slug}  {DIM}({area}) {title}{RST}")
    raw = input("pick #> ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(projects):
        return projects[int(raw) - 1][3]
    return None


# -------------------------------------------------------------- streaming ---
def stream_reply(messages, think, show_thinking):
    """POST to ollama with stream=True; print tokens live. Returns the answer
    text (the reply content only, not the thinking). Ctrl-C aborts the stream
    but not the REPL."""
    payload = {
        "model": MODEL,
        "messages": messages,
        "think": think,
        "stream": True,
        "options": {"temperature": 0.7, "num_ctx": 8192},
    }
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    answer, thought = [], []
    in_think = False
    t0 = datetime.datetime.now()
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            for raw in r:
                if not raw.strip():
                    continue
                chunk = json.loads(raw)
                msg = chunk.get("message", {})
                tdelta = msg.get("thinking")
                cdelta = msg.get("content")
                if tdelta:
                    thought.append(tdelta)
                    if show_thinking:
                        if not in_think:
                            sys.stdout.write(f"{DIM}⋯ ")
                            in_think = True
                        sys.stdout.write(tdelta)
                        sys.stdout.flush()
                if cdelta:
                    if in_think:
                        sys.stdout.write(f"{RST}\n")
                        in_think = False
                    answer.append(cdelta)
                    sys.stdout.write(cdelta)
                    sys.stdout.flush()
                if chunk.get("done"):
                    break
    except KeyboardInterrupt:
        sys.stdout.write(f"{RST}\n{YELLOW}[aborted]{RST}\n")
        return "".join(answer)
    if in_think:
        sys.stdout.write(RST)
    dt = (datetime.datetime.now() - t0).total_seconds()
    tag = "think" if think else "fast"
    twords = len("".join(thought).split())
    extra = f" · {twords}w thought" if twords else ""
    sys.stdout.write(f"\n{DIM}({dt:.1f}s · {tag} · {MODEL}{extra}){RST}\n")
    return "".join(answer)


# ------------------------------------------------------------------ saving ---
def save_block(md_path, text, label):
    stamp = datetime.date.today().isoformat()
    block = f"\n\n## AI session — {stamp} ({label})\n\n{text.strip()}\n"
    with open(md_path, "a") as f:
        f.write(block)
    print(f"{GREEN}✓ appended to {md_path.relative_to(KB)}{RST}")


HELP = f"""{BOLD}commands{RST} (a line starting with '.'):
  {CYAN}.t{RST}          toggle sticky THINK mode on/off
  {CYAN}?<msg>{RST}      think for just THIS message, then revert to current mode
  {CYAN}!<msg>{RST}      force FAST for just this message
  {CYAN}.m NAME{RST}     switch model (e.g. .m qwen3:8b  ·  .m qwen3.6:27b)
  {CYAN}.hush{RST}       hide/show the streamed thinking (answer only)
  {CYAN}.save{RST}       append the last answer into the project note
  {CYAN}.save all{RST}   append the whole conversation into the project note
  {CYAN}.p [SLUG]{RST}   switch project (no slug = picker)
  {CYAN}.reset{RST}      clear the conversation (keep the loaded project)
  {CYAN}.h{RST}          this help
  {CYAN}.q{RST}          quit  (Ctrl-D also quits)"""


# -------------------------------------------------------------------- repl ---
def main():
    global MODEL
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    md_path = find_project(args[0]) if args else pick_project()
    if not md_path:
        print("no project selected"); return

    def load(md):
        body = md.read_text()
        title = md.parent.name
        msgs = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content":
                f"We're working on my project note `{md.parent.name}`. Here it "
                f"is for context; help me flesh it out as we talk.\n\n---\n{body}\n---"},
            {"role": "assistant", "content":
                "Got it — I've read the note. What do you want to work on first?"},
        ]
        return title, msgs

    title, messages = load(md_path)
    sticky_think = False
    show_thinking = True

    print(f"{BOLD}cl chat{RST} · {CYAN}{title}{RST} · {DIM}{MODEL}{RST}")
    print(f"{DIM}fast by default. .t = think · ?msg = think once · .h = help · .q = quit{RST}")
    print(f"{DIM}I've loaded {md_path.relative_to(KB)}. Ask away.{RST}")

    while True:
        mode = "think" if sticky_think else "fast"
        color = YELLOW if sticky_think else GREEN
        try:
            raw = input(f"\n{color}{title} ·{mode}›{RST} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye"); return
        if not raw:
            continue

        # ---- dot-commands ----
        if raw.startswith("."):
            parts = raw.split(maxsplit=1)
            cmd, rest = parts[0], (parts[1] if len(parts) > 1 else "")
            if cmd in (".q", ".quit", ".exit"):
                print("bye"); return
            if cmd in (".h", ".help", ".?"):
                print(HELP); continue
            if cmd in (".t", ".think"):
                sticky_think = not sticky_think
                print(f"{DIM}sticky think {'ON' if sticky_think else 'OFF'}{RST}")
                continue
            if cmd in (".hush", ".quiet"):
                show_thinking = not show_thinking
                print(f"{DIM}stream thinking {'OFF' if not show_thinking else 'ON'}{RST}")
                continue
            if cmd in (".m", ".model"):
                if rest.strip():
                    MODEL = rest.strip()
                    print(f"{DIM}model → {MODEL}{RST}")
                else:
                    print(f"{DIM}model is {MODEL}{RST}")
                continue
            if cmd in (".reset", ".clear"):
                title, messages = load(md_path)
                print(f"{DIM}conversation reset (project kept){RST}")
                continue
            if cmd in (".p", ".proj", ".project"):
                new = find_project(rest.strip()) if rest.strip() else pick_project()
                if new:
                    md_path = new
                    title, messages = load(md_path)
                    print(f"{DIM}→ {title}  ({md_path.relative_to(KB)}){RST}")
                else:
                    print(f"{DIM}no project matched{RST}")
                continue
            if cmd == ".save":
                if rest.strip() == "all":
                    convo = "\n\n".join(
                        f"**{m['role']}:** {m['content']}"
                        for m in messages[3:])  # skip system + seeded context
                    if convo.strip():
                        save_block(md_path, convo, "transcript")
                    else:
                        print(f"{DIM}nothing to save yet{RST}")
                else:
                    last = next((m["content"] for m in reversed(messages)
                                 if m["role"] == "assistant"), "")
                    if last and last != messages[2]["content"]:
                        save_block(md_path, last, "answer")
                    else:
                        print(f"{DIM}no answer to save yet{RST}")
                continue
            print(f"{DIM}unknown command {cmd} — .h for help{RST}")
            continue

        # ---- per-turn think override ----
        think = sticky_think
        if raw[0] in "?!":
            think = raw[0] == "?"
            raw = raw[1:].strip()
            if not raw:
                continue

        messages.append({"role": "user", "content": raw})
        reply = stream_reply(messages, think, show_thinking)
        messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    sys.argv = ["chat"] + sys.argv[1:]
    main()
