# @kevin-david's dotfiles

Forked from [@holman's dotfiles](https://github.com/holman/dotfiles). More info in that repo.

## install

Run this:

```sh
git clone https://github.com/kevin-david/dotfiles.git ~/.dotfiles
cd ~/.dotfiles
script/bootstrap
```

This will symlink the appropriate files in `.dotfiles` to your home directory.
Everything is configured and tweaked within `~/.dotfiles`.

Atuin settings live in `atuin/config.toml`. Both `script/bootstrap` and
`script/install` link them to `~/.config/atuin/config.toml` (respecting
`XDG_CONFIG_HOME` or `ATUIN_CONFIG_DIR`). Existing configs are backed up beside
the destination before linking. To install just these settings, run
`./atuin/install.sh`.

The main file you'll want to change right off the bat is `zsh/zshrc.symlink`,
which sets up a few paths that'll be different on your particular machine.

## Tmux session recovery

Tmux Resurrect and Continuum save layouts, working directories and pane history,
with snapshots separated by hostname under `~/.local/state/tmux/resurrect/`.
The last snapshot is restored when tmux starts. Applications are resumed
manually from their own saved state.

On a Linux system with a user systemd manager, run
`./tmux/install.sh --enable-session-restore`. This enables tmux at user-manager
startup and a five-minute save timer that also works with all clients detached.
Run `sudo loginctl enable-linger "$USER"` for startup at boot without logging in.
The installer reloads existing tmux sessions without restarting their processes.
Use `systemctl --user start tmux-save.service` for an immediate snapshot.

`dot` is a simple script that installs some dependencies, sets sane macOS
defaults, and so on. Tweak this script, and occasionally run `dot` from
time to time to keep your environment fresh and up-to-date. You can find
this script in `bin/`.


## Linux resource triage

Run `sudo ~/.dotfiles/bin/hogs` on a cgroup v2 Linux host. The header appears
before Docker lookups. Environment and nested Docker totals are shown by
default, with measured CPU sample durations, inherited CPU quotas, effective
cpusets, quota-throttling deltas, and cgroup memory usage. Parent totals include
their children; do not add them together. Non-Docker residuals include jobs
launched directly in a container or user session.

The memory table includes the shared LXC parent and local high/max/OOM event
deltas. Reclaim can be caused by a cgroup limit even when the host has available
RAM. Name and restart lookups have an eight-second budget per environment per
pass; failures retain cgroup IDs and report incomplete metadata. Restart
observations compare two reads instead of treating a recently started process
with old failures as a confirmed loop.

`hogs 10` limits ranked rows. `--by-env` remains accepted; `--mem` adds process
RSS details. The Bash entry point uses the adjacent `bin/hogs-cgroups.py` helper
and Python 3.8+ with no third-party packages. Regression checks:
`uv run --no-project python -m unittest discover -s tests -p test_hogs.py`.

## Review model fallback

`ai-tools/multi_model_review.py` uses Opus by default for the Claude lane. `--claude-model` or `REVIEW_CLAUDE_MODEL` overrides the primary model. A failed initial Claude invocation is retried once when the fallback model differs from the primary. This covers nonzero exits, CLI error envelopes, unavailable models, and usage or spend limits, including limit messages returned with exit code zero. The fallback also defaults to Opus; `--claude-fallback-model` or `REVIEW_CLAUDE_FALLBACK_MODEL` overrides it.

The retry stays within the Claude lane and preserves both attempt diagnostics. A failed fallback leaves that lane failed; it does not restart completed reviewers. Same-session follow-ups retain their recorded model and do not switch models. Completed structured reviews are not retried merely because their findings mention a limit.
