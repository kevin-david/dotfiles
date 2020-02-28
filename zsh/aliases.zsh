alias reload!='. ~/.zshrc'

alias cls='clear' # Good 'ol Clear Screen command

alias df='/bin/df -h'
alias man='LC_ALL=C LANG=C man'
alias ll='ls -al'

if [ "$(uname -s)" = "Linux" ]
then
    alias ls='ls --color=auto -CFX'
    alias whois='whois -H'
fi
