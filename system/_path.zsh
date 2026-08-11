export PATH="./bin:$HOME/.local/bin:/usr/local/bin:/usr/local/sbin:$ZSH/bin:$PATH"
export MANPATH="/usr/local/man:/usr/local/mysql/man:/usr/local/git/man:$MANPATH"

# Resolve `codex` from its own managed-install root, never from the shim
# Codex's updater drops in ~/.local/bin. That shim is an absolute path under
# whichever host's CODEX_HOME updated last, so wherever ~/.local is shared
# between hosts it dangles on all the others.
#
# Default the root rather than keying off CODEX_HOME being set: a host that
# shares ~/.local but keeps the default state root has no CODEX_HOME, and was
# the one machine still resolving through the broken shim.
#
# Must stay in this file, after the ~/.local/bin prepend above: a separate
# topic path.zsh loads in zshrc's first loop, and this file (named _path.zsh,
# so it misses that loop's */path.zsh pattern) would then prepend over it.
codex_bin="${CODEX_HOME:-$HOME/.codex}/packages/standalone/current/bin"
if [[ -x "$codex_bin/codex" ]]; then
  export PATH="$codex_bin:$PATH"
fi
unset codex_bin
