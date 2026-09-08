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

`dot` is a simple script that installs some dependencies, sets sane macOS
defaults, and so on. Tweak this script, and occasionally run `dot` from
time to time to keep your environment fresh and up-to-date. You can find
this script in `bin/`.
