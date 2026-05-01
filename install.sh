#!/usr/bin/env bash
# install.sh — bootstrap CLIfe on a new machine.
#
# What this does (per machine):
#  - Creates a Python venv at ~/clife/venv and installs requirements
#  - Adds ~/clife to PATH in your shell rc
#  - Wires up zsh tab-completion (~/.zsh/completions)
#  - Checks that required system packages are installed (mbsync, fzf, ...)
#  - Seeds config templates in ~/.config/life-os/ and ~/ if missing
#
# What you still have to do (printed at the end):
#  - Fill in API keys + CL_INGEST_MAILDIR in ~/.config/life-os/secrets.env
#  - Set the kb-capture Gmail user in ~/.mbsyncrc and save the app password
#    to ~/.config/mbsync/kb-capture-password (chmod 600)
#  - Make sure ~/kb/ exists (clone your kb repo)
#  - One-time: gcalcli init  (Google Calendar auth)

set -e

CLIFE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$CLIFE_DIR/venv"

echo "==> Installing CLIfe at $CLIFE_DIR"

if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found. Install Python 3 first."
    exit 1
fi

# 1. Python venv + deps
if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

echo "==> Installing Python dependencies"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$CLIFE_DIR/requirements.txt" --quiet

chmod +x "$CLIFE_DIR/cl"

# 2. PATH in shell rc
SHELL_RC=""
case "$SHELL" in
    *zsh)  SHELL_RC="$HOME/.zshrc" ;;
    *bash) SHELL_RC="$HOME/.bashrc" ;;
esac

if [ -n "$SHELL_RC" ] && [ -f "$SHELL_RC" ] && ! grep -q "$CLIFE_DIR" "$SHELL_RC" 2>/dev/null; then
    echo "==> Adding $CLIFE_DIR to PATH in $SHELL_RC"
    {
        echo ""
        echo "# CLIfe"
        echo "export PATH=\"$CLIFE_DIR:\$PATH\""
    } >> "$SHELL_RC"
    PATH_ADDED=1
fi

# 3. zsh tab-completion
ZSH_COMPLETIONS_DIR="$HOME/.zsh/completions"
case "$SHELL" in
    *zsh)
        mkdir -p "$ZSH_COMPLETIONS_DIR"
        if [ ! -e "$ZSH_COMPLETIONS_DIR/_cl" ]; then
            echo "==> Linking completion file into $ZSH_COMPLETIONS_DIR/_cl"
            ln -s "$CLIFE_DIR/completions/_cl" "$ZSH_COMPLETIONS_DIR/_cl"
            COMPLETION_INSTALLED=1
        fi
        if [ -f "$SHELL_RC" ] && ! grep -q "fpath.*\.zsh/completions" "$SHELL_RC" 2>/dev/null; then
            echo "==> Adding completions dir to fpath in $SHELL_RC"
            {
                echo ""
                echo "# CLIfe completions"
                echo "fpath=(\$HOME/.zsh/completions \$fpath)"
                echo "autoload -Uz compinit && compinit"
            } >> "$SHELL_RC"
            COMPLETION_INSTALLED=1
        fi
        ;;
esac

# 4. System packages (informational — not auto-installed)
echo
echo "==> Checking system packages"
MISSING_PKGS=()
for cmd in mbsync fzf nvim; do
    if ! command -v "$cmd" &> /dev/null; then
        MISSING_PKGS+=("$cmd")
    fi
done

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "   missing: ${MISSING_PKGS[*]}"
    if command -v pacman &> /dev/null; then
        # Map command names to Arch package names
        ARCH_PKGS=()
        for cmd in "${MISSING_PKGS[@]}"; do
            case "$cmd" in
                mbsync) ARCH_PKGS+=("isync") ;;
                fzf)    ARCH_PKGS+=("fzf") ;;
                nvim)   ARCH_PKGS+=("neovim") ;;
                *)      ARCH_PKGS+=("$cmd") ;;
            esac
        done
        echo "   install with:  sudo pacman -S ${ARCH_PKGS[*]}"
    elif command -v apt &> /dev/null; then
        echo "   install with:  sudo apt install isync fzf neovim"
    elif command -v pkg &> /dev/null; then
        echo "   (Termux) install with:  pkg install isync fzf neovim"
    else
        echo "   install via your package manager"
    fi
else
    echo "   all required packages present"
fi

# 5. Seed config templates if missing
LIFEOS_CFG="$HOME/.config/life-os"
mkdir -p "$LIFEOS_CFG" "$HOME/.config/mbsync"

if [ ! -f "$LIFEOS_CFG/secrets.env" ]; then
    cp "$CLIFE_DIR/templates/secrets.env.example" "$LIFEOS_CFG/secrets.env"
    chmod 600 "$LIFEOS_CFG/secrets.env"
    echo "==> Seeded $LIFEOS_CFG/secrets.env  (fill in API keys)"
    SECRETS_SEEDED=1
fi

if [ ! -f "$HOME/.mbsyncrc" ]; then
    cp "$CLIFE_DIR/templates/mbsyncrc.example" "$HOME/.mbsyncrc"
    echo "==> Seeded ~/.mbsyncrc  (set User, save app password to ~/.config/mbsync/kb-capture-password)"
    MBSYNC_SEEDED=1
fi

# 6. Final report
echo
echo "==> Done."
echo
echo "Next steps:"
step=1
if [ "${PATH_ADDED:-0}" = "1" ] || [ "${COMPLETION_INSTALLED:-0}" = "1" ]; then
    echo "  $step. Reload shell:  source $SHELL_RC  (or open a new terminal)"
    step=$((step + 1))
fi
if [ "${SECRETS_SEEDED:-0}" = "1" ]; then
    echo "  $step. Fill in API keys in $LIFEOS_CFG/secrets.env"
    step=$((step + 1))
fi
if [ "${MBSYNC_SEEDED:-0}" = "1" ]; then
    echo "  $step. Edit ~/.mbsyncrc — set User to your kb-capture address"
    step=$((step + 1))
    echo "  $step. Save the Gmail app password:"
    step=$((step + 1))
    echo "       echo -n 'xxxx xxxx xxxx xxxx' > ~/.config/mbsync/kb-capture-password"
    echo "       chmod 600 ~/.config/mbsync/kb-capture-password"
fi
echo "  $step. Make sure ~/kb/ is cloned"
step=$((step + 1))
echo "  $step. First sync + ingest:  mbsync kb-capture && cl ingest"
step=$((step + 1))
echo "  $step. Test:  cl --help  and  cl <tab>"
