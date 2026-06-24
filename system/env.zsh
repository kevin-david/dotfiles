# --wait blocks until the file is closed (git commit/rebase need this, or they
# proceed before you've edited); --new opens a dedicated window instead of
# tacking a tab onto an existing one.
if [ "$(uname -s)" = "Darwin" ]; then
    export EDITOR='zed --wait --new'
else
    export EDITOR='nano -w'
fi

# ssh/mosh forward only TERM, dropping COLORTERM; restore it so truecolor-aware
# programs (and oh-my-tmux's auto detection) emit 24-bit color on remote shells.
# Gated on TERM so we don't assert truecolor on a dumb console (e.g. PVE serial).
case $TERM in
  *-ghostty|xterm-256color|tmux-256color|screen-256color|alacritty|*-direct)
    #temp disable - code(r) performance issues
    #export COLORTERM="${COLORTERM:-truecolor}" ;;
esac
