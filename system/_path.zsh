# $ZSH/bin goes ahead of ~/.local/bin so this repo's wrappers win over whatever
# a tool installs there. That matters for `codex`: Codex writes its own
# ~/.local/bin/codex "PATH alias" on every install or update, as a path absolute
# under the CODEX_HOME that wrote it — so anywhere ~/.local is shared between
# machines, that alias dangles on all but the last one to update. bin/codex
# resolves the managed install from CODEX_HOME itself and must be found first.
export PATH="./bin:$ZSH/bin:$HOME/.local/bin:/usr/local/bin:/usr/local/sbin:$PATH"
export MANPATH="/usr/local/man:/usr/local/mysql/man:/usr/local/git/man:$MANPATH"
