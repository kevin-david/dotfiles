# Let Codex's GitHub MCP server reuse the active GitHub CLI login.
# Keep an explicitly provided token (for example, in CI) unchanged.
if [[ -z ${GITHUB_PAT_TOKEN:-} ]] && command -v gh >/dev/null 2>&1; then
  GITHUB_PAT_TOKEN="$(gh auth token 2>/dev/null)" && export GITHUB_PAT_TOKEN
fi
