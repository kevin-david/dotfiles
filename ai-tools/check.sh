#!/usr/bin/env bash
# Lint + type-check the ai-tools scripts to exchanger's house bar.
# Stdlib-only scripts, so this runs via uvx on the host — no Docker (unlike
# exchanger's scripts/ruff.sh + scripts/ty.sh, which mount the api-server image).
# Config (ruff select + ty all=error) lives in pyproject.toml alongside.
#
#   ./check.sh        # check + format-check + ty (read-only; nonzero on any failure)
#   ./check.sh fix    # ruff --fix and ruff format in place, then ty
set -euo pipefail
cd "$(dirname "$0")"

if [[ "${1:-}" == "fix" ]]; then
  uvx ruff check --fix .
  uvx ruff format .
  uvx ty check
else
  uvx ruff check .
  uvx ruff format --check .
  uvx ty check
fi
