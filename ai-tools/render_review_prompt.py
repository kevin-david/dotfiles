#!/usr/bin/env python3
"""Render review prompts from the shared review-rubric skill."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import NoReturn

REVIEW_KINDS = ("code", "plan")

MODE_TOKENS: dict[str, dict[str, str]] = {
    "code": {
        "ASSESSMENT_SCALE": "mergeable | mergeable-with-fixes | needs-rework",
        "FINDING_PATH_EXAMPLE": "src/path/to/file.ext",
        "FINDING_BODY_GUIDANCE": (
            "Why it is wrong, traced through the flow, plus a concrete fix. Markdown ok. "
            "Do not prepend the reviewer tag - the runner adds it."
        ),
        "MODE_NOT_FINDING_RULES": "\n".join(
            [
                "- Anything a linter, type-checker, compiler, or CI would catch.",
                "- Intentional functional changes that are the point of the PR.",
                "- General test or doc requests not tied to a concrete behavioral gap.",
            ]
        ),
        "MODE_REFUTATION_RULES": "\n".join(
            [
                "- Before calling something a regression, compare the base behavior against the head behavior.",
                "- If the behavior predates the diff, mark it Pre-existing and keep it out of the assessment.",
            ]
        ),
    },
    "plan": {
        "ASSESSMENT_SCALE": "sound | sound-with-fixes | needs-rework",
        "FINDING_PATH_EXAMPLE": "docs/path/to/plan.md",
        "FINDING_BODY_GUIDANCE": (
            "What is wrong and why, traced through the real code or plan step you checked, plus the "
            "concrete fix to the plan. Markdown ok. Do not prepend the reviewer tag - the runner adds it."
        ),
        "MODE_NOT_FINDING_RULES": "\n".join(
            [
                "- The fact that it is only a plan. Catching plan defects before implementation is the point.",
                "- Re-litigating a deliberate design choice unless you can show it is incorrect, unsafe, or "
                "rule-violating.",
                "- Vague requests for more detail or more tests not tied to a named gap.",
            ]
        ),
        "MODE_REFUTATION_RULES": "\n".join(
            [
                "- For a 'this will not work' claim, construct the concrete way it fails.",
                "- For a 'the plan forgot X' claim, search the whole plan and repo first.",
            ]
        ),
    },
}


def _die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _candidate_reference_dirs() -> list[Path]:
    env = os.environ.get("REVIEW_RUBRIC_DIR")
    candidates: list[Path] = []
    if env:
        root = Path(env).expanduser()
        candidates.append(root if root.name == "references" else root / "references")
    home = Path.home()
    candidates.extend(
        [
            home / ".claude" / "skills" / "review-rubric" / "references",
            home / ".agents" / "skills" / "review-rubric" / "references",
            home / ".codex" / "skills" / "review-rubric" / "references",
        ]
    )
    return candidates


def references_dir() -> Path:
    for candidate in _candidate_reference_dirs():
        if candidate.is_dir():
            return candidate
    searched = "\n".join(f"  - {p}" for p in _candidate_reference_dirs())
    raise FileNotFoundError(f"review-rubric references directory not found. Searched:\n{searched}")


def _read(refs: Path, name: str) -> str:
    path = refs / name
    if not path.exists():
        raise FileNotFoundError(f"review-rubric reference missing: {path}")
    return path.read_text().strip()


def _replace_tokens(text: str, tokens: dict[str, str]) -> str:
    for key, value in tokens.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    return text


def render_prompt(kind: str) -> str:
    review_kind = kind.lower()
    if review_kind not in REVIEW_KINDS:
        raise ValueError(f"unknown review kind {kind!r}; expected one of: {', '.join(REVIEW_KINDS)}")

    refs = references_dir()
    title = "code" if review_kind == "code" else "plan"
    parts = [
        f"# Multi-model {title} review - neutral prompt",
        (
            "This prompt is assembled from the `review-rubric` skill references. Keep it harness-neutral "
            "and repo-neutral: repo-specific rules come from the target repo itself."
        ),
        _read(refs, f"{review_kind}-review.md"),
        _read(refs, "shared-rubric.md"),
        _read(refs, "output-contract-json.md"),
    ]
    return _replace_tokens("\n\n".join(parts), MODE_TOKENS[review_kind]).rstrip() + "\n"


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
