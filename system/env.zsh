if [ "$(uname -s)" = "Darwin" ]; then
    export EDITOR='zed'
else
    export EDITOR='nano -w'
fi
