#!/usr/bin/env bash
# install.sh — bootstrap CLIfe on a new machine

set -e

CLIFE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$CLIFE_DIR/venv"

echo "==> Installing CLIfe at $CLIFE_DIR"

if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found. Install Python 3 first."
    exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "==> Creating venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

echo "==> Installing Python dependencies"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$CLIFE_DIR/requirements.txt" --quiet

chmod +x "$CLIFE_DIR/cl"

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

# Wire up zsh tab-completion if the user has the standard ~/.zsh/completions dir
ZSH_COMPLETIONS_DIR="$HOME/.zsh/completions"
case "$SHELL" in
    *zsh)
        mkdir -p "$ZSH_COMPLETIONS_DIR"
        if [ ! -e "$ZSH_COMPLETIONS_DIR/_cl" ]; then
            echo "==> Linking completion file into $ZSH_COMPLETIONS_DIR/_cl"
            ln -s "$CLIFE_DIR/completions/_cl" "$ZSH_COMPLETIONS_DIR/_cl"
            COMPLETION_INSTALLED=1
        fi
        # Ensure ~/.zsh/completions is in fpath (most users already have this)
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

echo ""
echo "==> Done."
echo ""
echo "Next steps:"
if [ "${PATH_ADDED:-0}" = "1" ] || [ "${COMPLETION_INSTALLED:-0}" = "1" ]; then
    echo "  1. Reload shell:  source $SHELL_RC  (or open a new terminal)"
fi
echo "  2. Set GROQ_API_KEY in ~/.config/life-os/secrets.env  (for voice capture)"
echo "  3. Make sure ~/kb/ exists and is a git repo"
echo "  4. Test:  cl --help  and  cl <tab>"
