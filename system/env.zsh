# --wait blocks until the file is closed (git commit/rebase need this, or they
# proceed before you've edited); --new opens a dedicated window instead of
# tacking a tab onto an existing one.
if [ "$(uname -s)" = "Darwin" ]; then
    export EDITOR='zed --wait --new'
else
    export EDITOR='nano -w'
fi
