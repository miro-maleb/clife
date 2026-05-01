# Mobile setup (MVP — no native app)

This is the simplest way to use CLIfe from your phone for the next week of testing. A native Android app is a separate project (sub-project 09); this gets you 80% of the way without writing any phone code.

Two paths — start with Path A, add Path B if you want voice capture or grocery-list editing on your phone.

## Path A — Email bus (5 min setup)

**Use any email client on your phone.** Anything you want in your kb inbox: send or share-to-email it to your dedicated capture address (`miro.inbox.kb@gmail.com` for Miro). Next time you run `cl inbox` on your laptop, it auto-fetches and triages.

Setup:

1. Have a Gmail (or any email) client on your phone. The default Gmail app works.
2. Either:
   - **Compose:** open compose, To: capture address, body = whatever you want to capture, send.
   - **Share:** in any app (Twitter, browser, voice transcription, photo notes), tap Share → Email → set To: capture address, send.
3. Done. The next `cl inbox` on your laptop runs `mbsync kb-capture && cl ingest` automatically and the message lands in `kb/inbox/` ready to triage.

**Voice capture this way:** use Google Assistant or any voice-to-text app to dictate a note, then share-to-email the result.

**WhatsApp/SMS:** long-press a message → Share → Email → send to capture address. Manual but reliable.

**iOS:** the share sheet works the same. Anywhere there's a Share menu, Email is in there.

That's it. No Termux, no APK, no setup beyond having an email account.

## Path B — Termux (optional, ~20 min setup)

For when you want:
- Native voice capture (using your phone's mic + Whisper/Groq) directly to inbox
- The ability to run `cl inbox`, `cl projects`, etc. from your phone
- Editing grocery lists or notes in Termux's `nvim`

### 1. Install Termux

**Critical:** install from [F-Droid](https://f-droid.org/packages/com.termux/), NOT Google Play. The Google Play version is unmaintained.

Also install **Termux:API** (separate F-Droid package — needed for microphone access).

### 2. Inside Termux

```bash
pkg update
pkg install python git openssh termux-api fzf nano
```

(Use `nano` instead of `nvim` on the phone — it's lighter and the editor env var fallback handles it. If you want nvim, `pkg install neovim`.)

### 3. Set up GitHub auth

You need to clone `clife` (public, no auth needed) and your `kb` repo (private, needs auth).

For HTTPS with a token:

```bash
gh auth login   # if you have gh installed
# OR manually use a Personal Access Token when git prompts for password
```

For SSH (recommended):

```bash
ssh-keygen -t ed25519 -C "termux-phone"
cat ~/.ssh/id_ed25519.pub   # copy this
# add it to https://github.com/settings/keys as a new SSH key
```

### 4. Clone the repos

```bash
cd ~
git clone https://github.com/miro-maleb/clife.git
git clone git@github.com:miro-maleb/kb.git    # SSH, requires step 3
cd clife
./install.sh
```

`install.sh` will detect Termux (no zsh by default, `nano` editor) and set things up accordingly.

### 5. Set GROQ_API_KEY

Edit `~/.config/life-os/secrets.env` and put your key in:

```
export GROQ_API_KEY="gsk_..."
```

Source it: `source ~/.config/life-os/secrets.env` (or restart Termux).

### 6. Voice capture

Make sure the mic permission is granted to Termux:API.

```bash
cl capture --voice
```

Speak. Press Enter when done. The audio goes to Groq, comes back as text, lands in `~/kb/inbox/`.

### 7. Sync

After capturing on phone, push to GitHub:

```bash
cd ~/kb && git add -A && git commit -m "phone capture" && git push
```

Then on your laptop, `git pull` (or rely on `kbsync` if you've aliased it) — and the captures show up in `cl inbox`.

There's a `kbsync` alias mentioned in your global CLAUDE.md — set it up in Termux too if you want one-shot sync.

### 8. Grocery list

Edit `~/kb/shopping/grocery.md` directly in Termux's `nano` or `nvim`. Or pair Termux with Obsidian Mobile pointed at the same `~/kb/` directory and use Obsidian's friendlier UI.

## Notes & limitations

- Termux is text-only. You can't get a real Android UI (lock-screen widget, share-target intent) without building a native app — that's sub-project 09.
- Power users add a **Tasker** rule to one-shot voice capture without opening Termux. Not covered here.
- Email-bus latency: `cl ingest` runs on every `cl inbox`. Mail typically arrives in <2 min via mbsync's background fetch (or whenever you next trigger it).

When you've used this for a week and identified what's actually missing, sub-project 09 (native Android) becomes a real spec instead of speculation.
