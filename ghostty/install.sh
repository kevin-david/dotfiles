#!/usr/bin/env bash
#
# Ghostty
#
# Symlinks Ghostty configuration for macOS.

set -e

if [ "$(uname -s)" = "Darwin" ]; then
  echo "  Linking Ghostty config..."
  config_source="$HOME/.dotfiles/ghostty/ghostty-mac.conf"

  mkdir -p "$HOME/.config/ghostty"
  ln -sf "$config_source" "$HOME/.config/ghostty/ghostty.conf"

  mkdir -p "$HOME/Library/Application Support/com.mitchellh.ghostty"
  ln -sf "$config_source" "$HOME/Library/Application Support/com.mitchellh.ghostty/config.ghostty"

  echo "  Done with Ghostty config"
fi
