if [[ -f "$HOME/.atuin/bin/env" ]]
then
  . "$HOME/.atuin/bin/env"
fi

if command -v atuin >/dev/null 2>&1; then
  eval "$(atuin init zsh)"
fi
