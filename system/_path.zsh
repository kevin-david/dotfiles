export PATH="./bin:$HOME/.local/bin:/usr/local/bin:/usr/local/sbin:$ZSH/bin:$PATH"
export MANPATH="/usr/local/man:/usr/local/mysql/man:/usr/local/git/man:$MANPATH"

# Hosts that isolate their Codex state root (CODEX_HOME set in ~/.zshenv) must
# resolve `codex` from that root. The shim Codex's updater writes into the
# shared ~/.local/bin is an absolute path under whichever host's CODEX_HOME
# updated last, so it dangles on the others. See kevin-david/brain#19.
# Must stay in this file, after the ~/.local/bin prepend above: a separate
# topic path.zsh loads in zshrc's first loop, and this file (named _path.zsh,
# so it misses that loop's */path.zsh pattern) would then prepend over it.
if [[ -n "$CODEX_HOME" ]]; then
  export PATH="$CODEX_HOME/packages/standalone/current/bin:$PATH"
fi
