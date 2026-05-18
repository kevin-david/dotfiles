#!/usr/bin/env bash
#
# Ghostty
#
# Symlinks Ghostty configuration for macOS.

set -e

if [ "$(uname -s)" = "Darwin" ]; then
  echo "  Linking Ghostty config..."
  mkdir -p "$HOME/.config/ghostty"
  ln -sf "$HOME/.dotfiles/ghostty/ghostty-mac.conf" "$HOME/.config/ghostty/ghostty.conf"
  echo "  Done with Ghostty config"
fi
