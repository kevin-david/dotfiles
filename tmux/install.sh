#!/usr/bin/env bash
#
# Tmux
#
# Installs Oh my tmux! and symlinks tmux configuration.

set -e

oh_my_tmux_dir="$HOME/.local/share/tmux/oh-my-tmux"

if [ ! -d "$oh_my_tmux_dir/.git" ]; then
  echo "  Cloning Oh my tmux!..."
  mkdir -p "$(dirname "$oh_my_tmux_dir")"
  git clone --quiet --single-branch https://github.com/gpakosz/.tmux.git "$oh_my_tmux_dir"
fi

echo "  Linking tmux config..."

# Ensure the directory exists
mkdir -p "$HOME/.config/tmux"

# Symlink the files
ln -sf "$oh_my_tmux_dir/.tmux.conf" "$HOME/.config/tmux/tmux.conf"
ln -sf "$HOME/.dotfiles/tmux/tmux.conf.local" "$HOME/.config/tmux/tmux.conf.local"
