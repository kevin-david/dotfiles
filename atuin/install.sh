#!/usr/bin/env bash
# Symlink Atuin settings, preserving any existing config.

set -eu

config_source="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/config.toml"
config_dir="${ATUIN_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/atuin}"
config_target="$config_dir/config.toml"

mkdir -p "$config_dir"
if [ -L "$config_target" ] && [ "$(readlink "$config_target")" = "$config_source" ]; then
  exit 0
fi

if [ -e "$config_target" ] || [ -L "$config_target" ]; then
  if [ -d "$config_target" ]; then
    echo "Atuin config path is a directory: $config_target" >&2
    exit 1
  fi
  backup_dir="$(mktemp -d "$config_dir/config-backup.XXXXXX")"
  mv "$config_target" "$backup_dir/config.toml"
  echo "  Saved existing Atuin config to $backup_dir/config.toml"
fi

ln -s "$config_source" "$config_target"
echo "  Linked Atuin config"
