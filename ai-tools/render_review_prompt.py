#!/usr/bin/env python3
"""Render review prompts from the shared review-rubric skill."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import NoReturn

REVIEW_KINDS = ("code", "plan")


def _die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _candidate_template_dirs() -> list[Path]:
    env = os.environ.get("REVIEW_RUBRIC_DIR")
    candidates: list[Path] = []
    if env:
        root = Path(env).expanduser()
        candidates.append(root if root.name == "templates" else root / "templates")
    home = Path.home()
    candidates.extend(
        [
            home / ".claude" / "skills" / "review-rubric" / "templates",
            home / ".agents" / "skills" / "review-rubric" / "templates",
            home / ".codex" / "skills" / "review-rubric" / "templates",
        ]
    )
    return candidates


def templates_dir() -> Path:
    for candidate in _candidate_template_dirs():
        if candidate.is_dir():
            return candidate
    searched = "\n".join(f"  - {p}" for p in _candidate_template_dirs())
    raise FileNotFoundError(f"review-rubric templates directory not found. Searched:\n{searched}")


def _read(templates: Path, name: str) -> str:
    path = templates / name
    if not path.exists():
        raise FileNotFoundError(f"review-rubric template missing: {path}")
    return path.read_text().strip()


def render_prompt(kind: str) -> str:
    review_kind = kind.lower()
    if review_kind not in REVIEW_KINDS:
        raise ValueError(f"unknown review kind {kind!r}; expected one of: {', '.join(REVIEW_KINDS)}")

    return _read(templates_dir(), f"{review_kind}-review.md").rstrip() + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("kind", choices=REVIEW_KINDS, help="prompt kind to render")
    ap.add_argument("--output", help="write the rendered prompt to a file instead of stdout")
    args = ap.parse_args()

    try:
        prompt = render_prompt(args.kind)
    except (FileNotFoundError, ValueError) as e:
        _die(str(e))

    if args.output:
        Path(args.output).expanduser().write_text(prompt)
    else:
        print(prompt, end="")


if __name__ == "__main__":
    main()
