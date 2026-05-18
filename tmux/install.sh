#!/usr/bin/env bash
#
# Tmux
#
# Symlinks tmux configuration.

set -e

echo "  Linking tmux config..."

# Ensure the directory exists
mkdir -p "$HOME/.config/tmux"

# Symlink the file
ln -sf "$HOME/.dotfiles/tmux/tmux.conf.local" "$HOME/.config/tmux/tmux.conf.local"
