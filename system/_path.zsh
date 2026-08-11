# No `./bin` here. It was a relative entry sitting first, so any directory you
# cd into that happened to contain bin/ shadowed every command on the system —
# `cd some-repo && make` would run that repo's bin/make without so much as a
# `./`. Run project-local tools as ./bin/<name> explicitly instead.
#
# $ZSH/bin goes ahead of ~/.local/bin so this repo's wrappers win over whatever
# a tool installs there. That matters for `codex`: Codex writes its own
# ~/.local/bin/codex "PATH alias" on every install or update, as a path absolute
# under the CODEX_HOME that wrote it — so anywhere ~/.local is shared between
# machines, that alias dangles on all but the last one to update. bin/codex
# resolves the managed install from CODEX_HOME itself and must be found first.
export PATH="$ZSH/bin:$HOME/.local/bin:/usr/local/bin:/usr/local/sbin:$PATH"
export MANPATH="/usr/local/man:/usr/local/mysql/man:/usr/local/git/man:$MANPATH"
