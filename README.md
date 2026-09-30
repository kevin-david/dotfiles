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


## Review model fallback

`ai-tools/multi_model_review.py` uses Opus by default for the Claude lane. `--claude-model` or `REVIEW_CLAUDE_MODEL` overrides the primary model. A failed initial Claude invocation is retried once when the fallback model differs from the primary. This covers nonzero exits, CLI error envelopes, unavailable models, and usage or spend limits, including limit messages returned with exit code zero. The fallback also defaults to Opus; `--claude-fallback-model` or `REVIEW_CLAUDE_FALLBACK_MODEL` overrides it.

The retry stays within the Claude lane and preserves both attempt diagnostics. A failed fallback leaves that lane failed; it does not restart completed reviewers. Same-session follow-ups retain their recorded model and do not switch models. Completed structured reviews are not retried merely because their findings mention a limit.
