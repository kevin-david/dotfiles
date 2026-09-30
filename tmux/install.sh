#!/usr/bin/env bash
#
# Tmux
#
# Installs Oh my tmux! and symlinks tmux configuration.

set -e

case "${1:-}" in
  --verify-session-restore)
    # Separate sockets and snapshots keep this check away from live sessions.
    restore_check_dir=$(mktemp -d)
    save_socket="restore-save-$$"
    restore_socket="restore-load-$$"
    cleanup_restore_check() {
      tmux -L "$save_socket" kill-server 2>/dev/null || true
      tmux -L "$restore_socket" kill-server 2>/dev/null || true
      rm -rf -- "$restore_check_dir"
    }
    trap cleanup_restore_check EXIT
    resurrect_scripts="$HOME/.config/tmux/plugins/tmux-resurrect/scripts"
    tmux -L "$save_socket" -f /dev/null new-session -d -s restore-check -c "$restore_check_dir"
    tmux -L "$save_socket" set -g @resurrect-dir "$restore_check_dir/snapshots"
    tmux -L "$save_socket" set -g @resurrect-capture-pane-contents on
    tmux -L "$save_socket" split-window -h -t restore-check -c /tmp
    tmux -L "$save_socket" run-shell "'$resurrect_scripts/save.sh' quiet"
    test -s "$restore_check_dir/snapshots/last"
    tmux -L "$save_socket" kill-server

    tmux -L "$restore_socket" -f /dev/null new-session -d
    tmux -L "$restore_socket" set -g @resurrect-dir "$restore_check_dir/snapshots"
    tmux -L "$restore_socket" set -g @resurrect-processes false
    tmux -L "$restore_socket" set -g @resurrect-capture-pane-contents on
    tmux -L "$restore_socket" run-shell "'$resurrect_scripts/restore.sh'"
    restored_paths=$(tmux -L "$restore_socket" list-panes -t restore-check -F '#{pane_current_path}')
    test "$(printf '%s\n' "$restored_paths" | wc -l | tr -d ' ')" = 2
    printf '%s\n' "$restored_paths" | grep -Fx "$restore_check_dir" >/dev/null
    printf '%s\n' "$restored_paths" | grep -Fx /tmp >/dev/null
    echo 'Session save/restore passed: two panes and their working directories.'
    exit 0
    ;;
  ""|--enable-session-restore) ;;
  *) echo "Usage: $0 [--enable-session-restore|--verify-session-restore]" >&2; exit 2 ;;
esac

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

if [ "${1:-}" = --enable-session-restore ]; then
  # A user manager must already be available. Enable linger as root for boot
  # startup without an SSH login: loginctl enable-linger <user>.
  systemctl --user show-environment >/dev/null
  tmux_bin=$(command -v tmux)
  flock_bin=$(command -v flock)
  plugins_dir="$HOME/.config/tmux/plugins"
  units_dir="$HOME/.config/systemd/user"
  save_dir="$HOME/.local/state/tmux/resurrect/$(hostname)"
  mkdir -p "$plugins_dir" "$units_dir" "$save_dir"
  chmod 700 "$save_dir"

  for plugin in tpm tmux-resurrect tmux-continuum; do
    if [ ! -d "$plugins_dir/$plugin/.git" ]; then
      git clone --quiet --depth 1 "https://github.com/tmux-plugins/$plugin.git" "$plugins_dir/$plugin"
    fi
  done

  # Do not replace a service managed elsewhere.
  for unit in tmux-persistence.service tmux-save.service tmux-save.timer; do
    if [ -e "$units_dir/$unit" ] && ! grep -q '^# Managed by dotfiles/tmux/install.sh$' "$units_dir/$unit"; then
      echo "Refusing to replace unmanaged $units_dir/$unit" >&2
      exit 1
    fi
  done

  cat > "$units_dir/tmux-persistence.service" <<EOF
# Managed by dotfiles/tmux/install.sh
[Unit]
Description=Persistent tmux sessions
[Service]
Type=forking
Environment="PATH=$PATH"
WorkingDirectory=%h
ExecStart="$tmux_bin" new-session -d
ExecStop="$flock_bin" %t/tmux-save.lock "$plugins_dir/tmux-resurrect/scripts/save.sh" quiet
ExecStop="$tmux_bin" kill-server
TimeoutStopSec=30
[Install]
WantedBy=default.target
EOF

  cat > "$units_dir/tmux-save.service" <<EOF
# Managed by dotfiles/tmux/install.sh
[Unit]
Description=Save tmux sessions
[Service]
Type=oneshot
Environment="PATH=$PATH"
UMask=0077
ExecCondition="$tmux_bin" has-session
ExecStart="$flock_bin" %t/tmux-save.lock "$plugins_dir/tmux-resurrect/scripts/save.sh" quiet
EOF

  cat > "$units_dir/tmux-save.timer" <<'EOF'
# Managed by dotfiles/tmux/install.sh
[Unit]
Description=Save tmux sessions every five minutes, including when detached
[Timer]
OnActiveSec=5min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
EOF

  systemd-analyze --user verify "$units_dir/tmux-persistence.service" "$units_dir/tmux-save.service" "$units_dir/tmux-save.timer"
  systemctl --user daemon-reload
  systemctl --user enable tmux-persistence.service
  systemctl --user enable --now tmux-save.timer

  # Existing sessions keep their processes. The service takes over on next boot.
  if "$tmux_bin" has-session 2>/dev/null; then
    "$tmux_bin" source-file "$HOME/.config/tmux/tmux.conf"
  else
    systemctl --user start tmux-persistence.service
  fi
fi
