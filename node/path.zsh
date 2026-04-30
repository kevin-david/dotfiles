export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"  # This loads nvm
[ -s "$NVM_DIR/bash_completion" ] && \. "$NVM_DIR/bash_completion"

export NODE_BIN_PATH="$NVM_DIR/versions/node/$(nvm current)/bin"
export PATH="$NODE_BIN_PATH:$PATH"