#!/usr/bin/env python3
"""Fan a single PR out to multiple review harnesses (Claude Code, Codex, Antigravity).

Each harness runs headless against the same neutral prompt rendered from the
shared review-rubric skill and emits structured findings; this runner posts them
as INLINE review comments anchored to the changed lines, so each finding becomes
its own resolvable thread on the PR. Cross-cutting findings are anchored once, at
the reviewer's chosen best location.

Cross-harness by construction: orchestration lives here, not inside any one
CLI's skill/workflow system, so adding a harness is one entry in LANES.

Posting policy:
  - Public repos          -> report-only by default (opt in with --post).
  - Private/internal repos -> post automatically ("let it fly").
  - --post / --report override the visibility default.

GitHub access is via the `gh` CLI (reuses your existing auth — no token
handling, no extra dependency). Stdlib only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from io import TextIOWrapper
from pathlib import Path
from subprocess import CompletedProcess
from typing import Literal, NamedTuple, NewType, NoReturn, TypedDict, cast

import render_review_prompt

# Brands for the two bare-str ids that get passed positionally and would
# silently swap: a git commit id and a git ref name read the same to the type
# checker as any other str, so `diff(base, head)` with the args flipped would
# type-check fine. NewType makes that a type error.
Sha = NewType("Sha", str)  # a git commit id (head, base tip, merge-base, commit_id)
Ref = NewType("Ref", str)  # a git ref name (base branch)

Mode = Literal["post", "report"]
Severity = Literal["Critical", "Important", "Suggestion"]


class LaneResult(NamedTuple):
    """A lane's outcome: its final text plus the exit code / stderr of the CLI, so a
    crashed lane (bad auth, untrusted dir) is told apart from one that ran fine
    and found nothing. Without the code, both look like "no output"."""

    out: str
    code: int
    err: str


class Finding(TypedDict, total=False):
    """One harness-emitted finding. ``total=False`` because this is the untrusted
    JSON seam: any key may be absent, which is exactly what the inline-vs-summary
    routing and ``_as_int`` guards already handle per-field."""

    path: str
    line: int
    start_line: int
    severity: Severity
    title: str
    confidence: float
    body: str


class InspectionTarget(TypedDict, total=False):
    path: str
    symbols: list[str]
    conclusion: str


class ChangeComponent(TypedDict, total=False):
    name: str
    role: str


class ChangeMap(TypedDict, total=False):
    components: list[ChangeComponent]
    mermaid: str


class LaneReview(TypedDict, total=False):
    """A lane's full parsed review block. ``total=False`` pairs with the
    ``REQUIRED_KEYS`` presence check: missing sections are detected and flagged,
    not assumed present."""

    eligible: bool
    behavioral_delta: str
    inspected: list[InspectionTarget]
    coverage_gaps: list[str]
    change_map: ChangeMap
    method: str
    assessment: str
    strengths: list[str]
    description_notes: list[str]
    findings: list[Finding]


@dataclass
class ReviewCtx:
    """Threaded through lane processing and report building: the run's mode and
    target, the set of commentable diff lines, and the accumulating lane tallies."""

    mode: Mode
    slug: str
    pr: str
    head: Sha
    diff_lines: dict[str, set[int]]
    pr_title: str
    worktree: Path
    repo_files: set[str] = field(default_factory=set)
    review_overviews: list[tuple[str, LaneReview]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)
    inline_post_failed: bool = False


DEFAULT_REVIEW_KIND = "code"
SENTINEL_OPEN = "<<<REVIEW_JSON"
SENTINEL_CLOSE = "REVIEW_JSON>>>"


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# Confidence is evidentiary-only (see the review-rubric templates): 50 admits
# labelled `Unverified:` leads, and no threshold value filters by impact —
# severity is reported, never used as a gate here.
THRESHOLD = int(env("REVIEW_THRESHOLD", "50"))
HEARTBEAT_SECS = int(env("REVIEW_HEARTBEAT_SECS", "30"))
ANTIGRAVITY_SCRATCH_ROOT = (Path.home() / ".gemini" / "antigravity-cli" / "scratch").resolve()
LANE_LABELS = {
    "claude": env("REVIEW_CLAUDE_LABEL", "Claude"),
    "codex": env("REVIEW_CODEX_LABEL", "Codex"),
    "antigravity": env("REVIEW_ANTIGRAVITY_LABEL", "Antigravity"),
}
LANE_MODELS = {
    "claude": env("REVIEW_CLAUDE_MODEL", "fable"),
    "codex": env("REVIEW_CODEX_MODEL", "gpt-5.6-sol"),
    "antigravity": env("REVIEW_ANTIGRAVITY_MODEL", "Gemini 3.1 Pro (High)"),
}
LANE_EFFECTIVE_MODELS = dict(LANE_MODELS)
CLAUDE_FALLBACK_MODEL = env("REVIEW_CLAUDE_FALLBACK_MODEL", "opus")
LANE_EFFORTS = {
    "claude": env("REVIEW_CLAUDE_EFFORT", "high"),
    "codex": env("REVIEW_CODEX_EFFORT", "high"),
}


SEVERITY_RANK = {"Critical": 0, "Important": 1, "Suggestion": 2}
# Minimum structured inspection references that must resolve to a real file or
# symbol in the review worktree. Formatting in the prose `method` is not proof;
# the machine-readable paths and symbols are.
HOLLOW_INSPECTION_MIN_REFS = 2
# Every section the prompt forces a reviewer to emit. A block missing any of
# these didn't do the work — the lane is flagged incomplete and its verdict
# discounted, rather than silently trusted as a clean pass.
REQUIRED_KEYS = (
    "eligible",
    "behavioral_delta",
    "inspected",
    "coverage_gaps",
    "change_map",
    "method",
    "assessment",
    "strengths",
    "description_notes",
    "findings",
)
ANTIGRAVITY_REVIEW_SCHEMA = {
    "type": "object",
    "required": list(REQUIRED_KEYS),
    "properties": {
        "eligible": {"type": "boolean"},
        "behavioral_delta": {"type": "string"},
        "inspected": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "symbols", "conclusion"],
                "properties": {
                    "path": {"type": "string"},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "conclusion": {"type": "string"},
                },
            },
        },
        "coverage_gaps": {"type": "array", "items": {"type": "string"}},
        "change_map": {
            "type": "object",
            "required": ["components", "mermaid"],
            "properties": {
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "role"],
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                        },
                    },
                },
                "mermaid": {"type": "string"},
            },
        },
        "method": {"type": "string"},
        "assessment": {"type": "string"},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "description_notes": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "line", "severity", "title", "confidence", "body"],
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "start_line": {"type": ["integer", "null"]},
                    "severity": {
                        "type": "string",
                        "enum": ["Critical", "Important", "Suggestion"],
                    },
                    "title": {"type": "string"},
                    "confidence": {"type": "number"},
                    "body": {"type": "string"},
                },
            },
        },
    },
}
_ANTIGRAVITY_RETRYABLE_ERRORS = ("timeout waiting for response",)


def die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], *, cwd: str | Path | None = None, input: str | None = None) -> CompletedProcess[str]:
    """Run a command, capturing text output. Does not raise on nonzero."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=cwd, input=input)


def run_ok(cmd: list[str]) -> str:
    """Run a command that must succeed; return stdout."""
    p = run(cmd)
    if p.returncode != 0:
        die(f"command failed: {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout


def tag_for(lane: str) -> str:
    label = LANE_LABELS[lane]
    model = LANE_EFFECTIVE_MODELS[lane]
    effort = LANE_EFFORTS.get(lane)
    if model:
        if effort:
            return f"[{label} ({model} / {effort})]"
        return f"[{label} ({model})]"
    return f"[{label}]"


# --- prompt ------------------------------------------------------------------
_CHANGED_ROOT_COVERAGE_INSTRUCTION = """

Multi-review coverage contract:
- Inventory the changed paths before reading the full diff. For every changed top-level directory,
  inspect at least one changed path there or name that exact directory in `coverage_gaps`.
  Do not claim behavior for an uninspected surface.
"""


def _with_changed_root_coverage_instruction(prompt: str) -> str:
    if "For every changed top-level directory" in prompt:
        return prompt
    return prompt + _CHANGED_ROOT_COVERAGE_INSTRUCTION


def render_prompt(template: str, *, base: Sha, slug: str, head: Sha, pr: str, body: str) -> str:
    repl = {
        "{{BASE_SHA}}": base,
        "{{REPO_SLUG}}": slug,
        "{{HEAD_SHA}}": head,
        "{{PR_NUMBER}}": pr,
        "{{THRESHOLD}}": str(THRESHOLD),
    }
    for k, v in repl.items():
        template = template.replace(k, v)
    # PR_BODY last: its text is author-controlled and may itself contain a
    # `{{...}}` token, so substitute it after every real token is resolved.
    rendered = template.replace("{{PR_BODY}}", body.strip() or "(no description)")
    return _with_changed_root_coverage_instruction(rendered)


def load_prompt_template(prompt_path: Path | None, review_kind: str) -> str:
    if prompt_path is not None:
        if not prompt_path.exists():
            die(f"prompt template missing: {prompt_path}")
        return prompt_path.read_text()
    try:
        return render_review_prompt.render_prompt(review_kind)
    except ValueError as e:
        die(str(e))
    except FileNotFoundError as e:
        # review-rubric skill not installed (e.g. a fresh clone): fall back to
        # the in-repo snapshot, which the tests keep byte-for-byte in sync with
        # the rendered prompt.
        fallback = Path(__file__).resolve().parent / (
            "review-prompt.md" if review_kind == "code" else "review-prompt-plan.md"
        )
        if fallback.exists():
            return fallback.read_text()
        die(str(e))


# --- lanes -------------------------------------------------------------------
# Each lane runs a CLI headless in the worktree and returns its raw final text.
def _write_lane_prompt(out: Path, lane: str, prompt: str) -> Path:
    prompt_path = (out / f"{lane}.prompt").resolve()
    prompt_path.write_text(prompt)
    return prompt_path


def _prompt_file_instruction(prompt_path: Path) -> str:
    return f"Read the complete review instructions from `{prompt_path}` and follow them exactly."


def _claude_command(prompt: str, model: str) -> list[str]:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--permission-mode",
        "bypassPermissions",
        "--effort",
        LANE_EFFORTS["claude"],
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def _claude_model_unavailable(result: CompletedProcess[str]) -> bool:
    if SENTINEL_OPEN in result.stdout:
        return False
    response = f"{result.stdout}\n{result.stderr}".lower()
    reached_limit = "reached your" in response and " limit" in response
    missing_model = "model" in response and any(
        marker in response for marker in ("not available", "unavailable", "not found", "unknown model")
    )
    return reached_limit or missing_model


def lane_claude(prompt: str, wt: str, out: Path) -> LaneResult:
    prompt_path = _write_lane_prompt(out, "claude", prompt)
    prompt_instruction = _prompt_file_instruction(prompt_path)
    primary_model = LANE_MODELS["claude"]
    primary = run(_claude_command(prompt_instruction, primary_model), cwd=wt)
    attempts = [(primary_model, primary)]

    if _claude_model_unavailable(primary) and primary_model != CLAUDE_FALLBACK_MODEL:
        print(f"[claude] {primary_model or 'default'} unavailable; retrying once with {CLAUDE_FALLBACK_MODEL}")
        fallback = run(_claude_command(prompt_instruction, CLAUDE_FALLBACK_MODEL), cwd=wt)
        attempts.append((CLAUDE_FALLBACK_MODEL, fallback))
        if not _claude_model_unavailable(fallback):
            LANE_EFFECTIVE_MODELS["claude"] = CLAUDE_FALLBACK_MODEL
            primary = fallback
        else:
            primary = CompletedProcess(
                args=fallback.args,
                returncode=1,
                stdout="",
                stderr=fallback.stderr or fallback.stdout,
            )
    elif _claude_model_unavailable(primary):
        primary = CompletedProcess(
            args=primary.args,
            returncode=1,
            stdout="",
            stderr=primary.stderr or primary.stdout,
        )

    error_log = "\n\n".join(
        f"[{model or 'default'}]\n{attempt.stderr or (attempt.stdout if _claude_model_unavailable(attempt) else '')}"
        for model, attempt in attempts
    )
    (out / "claude.err").write_text(error_log)
    return LaneResult(primary.stdout, primary.returncode, primary.stderr)


def lane_codex(prompt: str, wt: str, out: Path) -> LaneResult:
    prompt_path = _write_lane_prompt(out, "codex", prompt)
    last = out / "codex.last"
    cmd = [
        "codex",
        "exec",
        "-s",
        "read-only",
        "-C",
        wt,
        "-c",
        f'model_reasoning_effort="{LANE_EFFORTS["codex"]}"',
        "--output-last-message",
        str(last),
    ]
    if LANE_MODELS["codex"]:
        cmd += ["-m", LANE_MODELS["codex"]]
    cmd += [_prompt_file_instruction(prompt_path)]
    p = run(cmd)
    (out / "codex.err").write_text(p.stderr)
    # codex writes its final message to `last`; stdout is the event log.
    text = last.read_text() if last.exists() else p.stdout
    return LaneResult(text, p.returncode, p.stderr)


def lane_antigravity(prompt: str, wt: str, out: Path) -> LaneResult:
    # The review worktree is a fresh throwaway dir Antigravity has never "trusted",
    # so it downgrades to default approval and refuses tool calls headlessly.
    # --dangerously-skip-permissions bypasses approval prompts.
    worktree = Path(wt).resolve()
    prompt = _with_changed_root_coverage_instruction(prompt)
    expected_head = run_ok(["git", "-C", str(worktree), "rev-parse", "HEAD"]).strip()
    provenance_cmd = f"git -C {shlex.quote(str(worktree))} rev-parse HEAD"
    instruction_path = (out / "antigravity.prompt").resolve()
    grounded_prompt = f"""\
Antigravity execution boundary:
- The checked-out review worktree is exactly `{worktree}`.
- The complete review contract is stored at `{instruction_path}`.
- If a context checkpoint truncates this request or points you to a conversation
  transcript, reread `{instruction_path}` instead. Never read or search Antigravity
  CLI transcripts, logs, or scratch state.
- Before inspecting anything, run exactly: `{provenance_cmd}`
- That first repository command must contain only that command. Do not append, redirect,
  pipe, or combine it with another command.
- Its output must be exactly `{expected_head}`. If it differs, stop and report failure.
- Run every later repository command with its Cwd inside `{worktree}`.
{prompt}

Antigravity final-output override:
- The CLI enforces a native JSON schema for the final result.
- Return only the JSON object required by that schema.
- Do not emit the `<<<REVIEW_JSON` / `REVIEW_JSON>>>` wrapper requested above.
"""
    instruction_path.write_text(grounded_prompt)
    schema_path = (out / "antigravity.schema.json").resolve()
    schema_path.write_text(json.dumps(ANTIGRAVITY_REVIEW_SCHEMA, separators=(",", ":")))
    for attempt in (1, 2):
        attempt_prompt = f"""\
Read the complete review instructions from `{instruction_path}` and follow them exactly.
Before inspecting the repository, run exactly: `{provenance_cmd}`
Its output must be exactly `{expected_head}`. If it differs, stop and report failure.
"""
        if attempt == 2:
            attempt_prompt += (
                "\nThis is a fresh retry after the prior model generation timed out. "
                "Inspect concisely, then emit the schema-constrained result once. "
                "Do not repeat filler or status words.\n"
            )
        cmd = [
            "agy",
            "-p",
            attempt_prompt,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--json-schema",
            str(schema_path),
            "--print-timeout",
            "10m",
        ]
        if LANE_MODELS["antigravity"]:
            cmd += ["--model", LANE_MODELS["antigravity"]]
        p = run(cmd, cwd=wt)
        if attempt == 1 and _antigravity_retryable_error(p.stdout) is not None:
            (out / "antigravity.attempt1.stream.jsonl").write_text(p.stdout)
            (out / "antigravity.attempt1.err").write_text(p.stderr)
            print("[antigravity] generation timed out; retrying once with a fresh conversation")
            continue

        (out / "antigravity.stream.jsonl").write_text(p.stdout)
        response, stream_error = _parse_antigravity_stream(
            p.stdout,
            worktree=worktree,
            provenance_cmd=provenance_cmd,
            expected_head=expected_head,
        )
        errors = [part for part in (p.stderr.strip(), stream_error) if part]
        if p.returncode != 0 and not errors:
            errors.append(f"agy exited {p.returncode} without a structured error")
        if errors:
            err = "\n".join(errors)
            if response.strip():
                (out / "antigravity.rejected.raw").write_text(response)
            (out / "antigravity.err").write_text(err)
            return LaneResult("", p.returncode or 1, err)
        wrapped = f"{SENTINEL_OPEN}\n{response}\n{SENTINEL_CLOSE}"
        (out / "antigravity.err").write_text("")
        return LaneResult(wrapped, 0, "")

    err = "Antigravity lane exhausted its generation retry without a result."
    (out / "antigravity.err").write_text(err)
    return LaneResult("", 1, err)


def _antigravity_retryable_error(raw: str) -> str | None:
    for line in raw.splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            result = json.loads(line).get("result")
            if not isinstance(result, dict):
                continue
            error = result.get("error")
            if isinstance(error, str) and any(marker in error.lower() for marker in _ANTIGRAVITY_RETRYABLE_ERRORS):
                return error
    return None


def _parse_antigravity_stream(
    raw: str,
    *,
    worktree: Path,
    provenance_cmd: str,
    expected_head: str,
) -> tuple[str, str | None]:
    """Extract the final response and prove Antigravity used the intended checkout."""
    provenance_ok = False
    response = ""
    result_status = ""
    result_error = ""
    outside_paths: set[str] = set()

    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        update = event.get("step_update")
        if isinstance(update, dict):
            tool = update.get("tool_info")
            if isinstance(tool, dict) and update.get("state") == "DONE":
                params = tool.get("parameters")
                if isinstance(params, dict):
                    command = params.get("CommandLine")
                    output = tool.get("output")
                    if command == provenance_cmd and isinstance(output, str):
                        provenance_ok = output.strip() == expected_head
                    for key, value in params.items():
                        if not isinstance(value, str) or not Path(value).is_absolute():
                            continue
                        normalized_key = key.lower()
                        if normalized_key not in {
                            "absolutepath",
                            "cwd",
                            "path",
                            "searchdirectory",
                            "searchpath",
                        }:
                            continue
                        resolved_path = Path(value).resolve()
                        if normalized_key in {"absolutepath", "path"} and (
                            _path_is_within(
                                resolved_path,
                                Path(tempfile.gettempdir()).resolve(),
                            )
                            or _path_is_within(
                                resolved_path,
                                ANTIGRAVITY_SCRATCH_ROOT,
                            )
                        ):
                            continue
                        try:
                            resolved_path.relative_to(worktree)
                        except ValueError:
                            outside_paths.add(value)
        result = event.get("result")
        if isinstance(result, dict):
            structured_output = result.get("structured_output")
            raw_response = result.get("response")
            if isinstance(structured_output, dict):
                response = json.dumps(structured_output)
            elif isinstance(raw_response, str):
                response = raw_response
            result_status = str(result.get("status", ""))
            error = result.get("error")
            if isinstance(error, str):
                result_error = error

    outside_paths = {path for path in outside_paths if not _path_is_within(Path(path).resolve(), worktree)}
    trust_issues: list[str] = []
    if not provenance_ok:
        trust_issues.append(
            "Antigravity lane rejected: it did not prove the review checkout with "
            f"`{provenance_cmd}` -> `{expected_head}`."
        )
    if outside_paths:
        paths = ", ".join(sorted(outside_paths))
        trust_issues.append(
            f"Antigravity lane rejected: repository tools escaped the review worktree: {paths}"
        )
    result_issues: list[str] = []
    if result_status != "SUCCESS":
        detail = f": {result_error}" if result_error else ""
        result_issues.append(
            f"Antigravity lane rejected: structured result status was {result_status or '(missing)'}{detail}."
        )
    if not response.strip():
        result_issues.append("Antigravity lane rejected: structured stream had no final response.")
    issues = [*trust_issues, *result_issues]
    if trust_issues:
        return "", "\n".join(issues)
    if result_issues:
        return response, "\n".join(result_issues)
    return response, None


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


LANES = {"claude": lane_claude, "codex": lane_codex, "antigravity": lane_antigravity}

LANE_BINARIES = {
    "claude": "claude",
    "codex": "codex",
    "antigravity": "agy",
}


# --- findings ----------------------------------------------------------------
def extract_findings(raw: str) -> LaneReview | None:
    """Return the last valid JSON object between review sentinels."""
    reviews: list[LaneReview] = []
    start = 0
    while True:
        sentinel = raw.find(SENTINEL_OPEN, start)
        if sentinel == -1:
            break
        body_start = raw.find("\n", sentinel)
        body_end = raw.find(SENTINEL_CLOSE, body_start)
        if body_start != -1 and body_end != -1:
            with contextlib.suppress(json.JSONDecodeError):
                reviews.append(json.loads(raw[body_start:body_end]))
        # Advance only past this opening sentinel so a complete block nested
        # after an abandoned attempt is still considered independently.
        start = sentinel + len(SENTINEL_OPEN)
    return reviews[-1] if reviews else None


def review_contract_issues(
    data: Mapping[str, object],
    repo_files: set[str],
    *,
    changed_files: set[str] | None = None,
) -> list[str]:
    issues: list[str] = []
    if not isinstance(data.get("behavioral_delta"), str) or not str(data["behavioral_delta"]).strip():
        issues.append("behavioral_delta must be non-empty")

    inspected = data.get("inspected")
    targets: set[tuple[str, str]] = set()
    inspected_paths: set[str] = set()
    unknown_paths: set[str] = set()
    if not isinstance(inspected, list):
        issues.append("inspected must be an array")
    else:
        for item in inspected:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            symbols = item.get("symbols")
            conclusion = item.get("conclusion")
            if isinstance(path, str):
                inspected_paths.add(path)
                if path not in repo_files:
                    unknown_paths.add(path)
                if isinstance(symbols, list):
                    targets.update((path, symbol) for symbol in symbols if isinstance(symbol, str) and symbol.strip())
            if not isinstance(conclusion, str) or not conclusion.strip():
                issues.append("each inspected entry needs a conclusion")
    if unknown_paths:
        issues.append(f"inspected paths not in worktree: {', '.join(sorted(unknown_paths))}")
    if len(targets) < 2:
        issues.append("inspected must name at least 2 verifiable file/symbol targets")

    coverage_gaps = data.get("coverage_gaps")
    if not isinstance(coverage_gaps, list) or not all(isinstance(gap, str) for gap in coverage_gaps):
        issues.append("coverage_gaps must be an array")
    elif changed_files:
        changed_roots = {path.split("/", 1)[0] for path in changed_files if "/" in path}
        inspected_roots = {path.split("/", 1)[0] for path in inspected_paths if "/" in path}
        gap_strings = [gap for gap in coverage_gaps if isinstance(gap, str)]
        gap_tokens = {token.lower() for gap in gap_strings for token in re.findall(r"[A-Za-z0-9_.-]+", gap)}
        unaccounted_roots = sorted(root for root in changed_roots - inspected_roots if root.lower() not in gap_tokens)
        if unaccounted_roots:
            issues.append(
                "changed top-level directories lack inspection or coverage gap: " + ", ".join(unaccounted_roots)
            )

    change_map = data.get("change_map")
    if not isinstance(change_map, dict):
        issues.append("change_map must be an object")
        return issues
    components = change_map.get("components")
    if not isinstance(components, list):
        issues.append("change_map.components must be an array")
        return issues
    for component in components:
        if not isinstance(component, dict):
            issues.append("each change_map component needs a name and role")
            break
        name = component.get("name")
        role = component.get("role")
        if not isinstance(name, str) or not name.strip() or not isinstance(role, str) or not role.strip():
            issues.append("each change_map component needs a name and role")
            break
    mermaid = change_map.get("mermaid")
    if not isinstance(mermaid, str):
        issues.append("change_map.mermaid must be a string")
    return issues


def partition_findings(raw: object, threshold: int) -> tuple[list[Finding], list[str]]:
    """Threshold-filter findings and enforce the lead-label contract: the
    templates make `Unverified:` mandatory for any finding kept at confidence
    <=50, so an unlabelled lead is exactly the asserted-as-established claim
    the scale exists to prevent — drop it and report its title instead of
    rendering it."""
    kept: list[Finding] = []
    dropped: list[str] = []
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        f = cast("Finding", f)
        conf = f.get("confidence", 100)
        if not isinstance(conf, (int, float)):
            conf = 100
        if conf < threshold:
            continue
        if conf <= 50 and "Unverified:" not in f"{f.get('title', '')}\n{f.get('body', '')}":
            dropped.append(str(f.get("title") or "untitled")[:60])
            continue
        kept.append(f)
    return kept, dropped


def render_review_overview(reviews: list[tuple[str, LaneReview]], head: Sha) -> str:
    lane, review = reviews[0]
    parts = ["## Multi-review change map", "", f"Reviewed head: `{head[:12]}` · map from `{lane}` lane", ""]
    parts += ["**Behavioral delta:**", str(review.get("behavioral_delta", "n/a")), ""]

    change_map = review.get("change_map") or {}
    components = change_map.get("components") or []
    if components:
        parts += ["| Component | Role |", "|---|---|"]
        for component in components:
            name = str(component.get("name", "")).replace("|", "\\|")
            role = str(component.get("role", "")).replace("|", "\\|")
            parts.append(f"| {name} | {role} |")
        parts.append("")
    mermaid = change_map.get("mermaid")
    if isinstance(mermaid, str) and mermaid.strip():
        parts += ["```mermaid", mermaid.strip(), "```", ""]

    gaps: list[str] = []
    for _, lane_review in reviews:
        for gap in lane_review.get("coverage_gaps") or []:
            if gap not in gaps:
                gaps.append(gap)
    parts.append("**Coverage gaps across lanes:**")
    parts += [f"- {gap}" for gap in gaps] if gaps else ["- None reported."]
    return "\n".join(parts)


def _as_int(v: object) -> int | None:
    """Coerce a model-supplied line number to int, or None if it isn't one."""
    if not isinstance(v, (int, float, str)):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _hunk_new_start(header: str) -> int:
    """New-file start line from a `@@ -a,b +c,d @@` hunk header (0 if unparsable)."""
    try:
        plus = header.split("+", 1)[1]
        return int(plus.split(",", 1)[0].split(" ", 1)[0].split("@@", 1)[0])
    except (IndexError, ValueError):
        return 0


def diff_commentable_lines(base: Sha, head: Sha) -> dict[str, set[int]]:
    """RIGHT-side line numbers GitHub will accept an inline comment on, per file:
    the added/context lines inside a hunk of ``base..head``. Used to route a
    finding to inline (its line is in the diff) vs. the summary (it isn't) WITHOUT
    firing a doomed POST and catching the 422. We track an ``in_hunk`` flag so an
    added content line that happens to read ``+++ x`` isn't mistaken for the
    ``+++ b/path`` file header (both start with ``+``; only the header appears
    outside a hunk)."""
    out = run_ok(["git", "diff", f"{base}..{head}"])
    files: dict[str, set[int]] = {}
    path: str | None = None
    newln = 0
    in_hunk = False
    for ln in out.splitlines():
        if ln.startswith("diff --git"):
            in_hunk, path = False, None
        elif not in_hunk:
            if ln.startswith("+++ "):
                p = ln[4:].strip()
                path = None if p == "/dev/null" else p.removeprefix("b/")
                if path:
                    files.setdefault(path, set())
            elif ln.startswith("@@"):
                newln = _hunk_new_start(ln)
                in_hunk = newln > 0
        elif ln.startswith("@@"):
            newln = _hunk_new_start(ln)
            in_hunk = newln > 0
        elif ln.startswith(("+", " ")):  # added or context line: both RIGHT-side commentable
            if path:
                files[path].add(newln)
            newln += 1
        elif not ln.startswith(("-", "\\")):
            in_hunk = False  # left the hunk region
    return files


def count_real_inspected_refs(raw: object, worktree: Path) -> int:
    """Count distinct structured paths and symbols that exist in the worktree."""
    hits: set[tuple[str, str]] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not (worktree / path).is_file():
            continue
        hits.add((path, ""))
        symbols = item.get("symbols")
        for symbol in symbols if isinstance(symbols, list) else []:
            if not isinstance(symbol, str):
                continue
            name = re.sub(r"\(.*\)$", "", symbol).rsplit(".", 1)[-1].strip()
            if re.fullmatch(r"\w+", name):
                command = ["git", "-C", str(worktree), "grep", "-q", "-F", name, "--", path]
                if run(command).returncode == 0:
                    hits.add((path, name))
    return len(hits)


def post_inline(slug: str, pr: str, head: Sha, f: Finding, body: str, line: int) -> CompletedProcess[str]:
    """One review comment per finding => its own resolvable thread."""
    cmd = [
        "gh",
        "api",
        "--method",
        "POST",
        f"repos/{slug}/pulls/{pr}/comments",
        "-f",
        f"commit_id={head}",
        "-f",
        f"path={f['path']}",
        "-f",
        "side=RIGHT",
        "-F",
        f"line={line}",
        "-f",
        f"body={body}",
    ]
    start = _as_int(f.get("start_line"))
    if start is not None:
        cmd += ["-f", "start_side=RIGHT", "-F", f"start_line={start}"]
    return run(cmd)


def process_lane(lane: str, res: LaneResult, ctx: ReviewCtx, out: Path) -> None:
    tag = tag_for(lane)
    raw = res.out
    data = extract_findings(raw)

    # A lane that produced no findings AND either exited nonzero or emitted
    # nothing didn't "find nothing" — it crashed (bad auth, untrusted dir,
    # network). Surface it loudly and don't post error noise to the PR; a clean
    # no-findings run still emits a JSON block, so data would not be None here.
    if data is None and (res.code != 0 or not raw.strip()):
        tail = (res.err or raw).strip().splitlines()[-4:] or ["(no stderr captured)"]
        print(f"[{lane}] FAILED — exit {res.code}, no findings produced. Last stderr:", file=sys.stderr)
        for line in tail:
            print(f"    {line}", file=sys.stderr)
        ctx.failed.append(lane)
        return

    if data is None:
        print(f"[{lane}] no valid JSON block; treating raw output as a summary")
        text = f"**{tag}** — automated review (unstructured output)\n\n{raw}"
        _post_or_print(lane, "raw summary", text, ctx)
        return

    # Presence check: a lane that dropped a required section didn't review to the
    # contract. Surface which keys are missing and flag the lane so its verdict is
    # read with suspicion (a "no findings / mergeable" from an incomplete pass is
    # not evidence). We still render what it did return.
    missing = [k for k in REQUIRED_KEYS if k not in data]
    incomplete_reasons: list[str] = []
    if missing:
        incomplete_reasons.append(f"missing section(s): {', '.join(missing)}")
    elif (refs := count_real_inspected_refs(data.get("inspected"), ctx.worktree)) < HOLLOW_INSPECTION_MIN_REFS:
        incomplete_reasons.append(f"hollow inspection: {refs} verifiable reference(s)")
    contract_issues = review_contract_issues(data, ctx.repo_files, changed_files=set(ctx.diff_lines))
    if contract_issues:
        incomplete_reasons.append(f"contract: {'; '.join(contract_issues)}")
    findings, unlabelled_leads = partition_findings(data.get("findings", []), THRESHOLD)
    if unlabelled_leads:
        incomplete_reasons.append(
            "dropped unlabelled lead finding(s) — confidence <=50 requires `Unverified:`: "
            + "; ".join(unlabelled_leads)
        )
    for reason in incomplete_reasons:
        print(f"  [{lane}] INCOMPLETE — {reason}", file=sys.stderr)
        ctx.incomplete.append(f"{lane} ({reason})")
    incomplete_reason = "; ".join(incomplete_reasons)

    findings.sort(key=lambda f: SEVERITY_RANK.get(f.get("severity", "Suggestion"), 3))
    # Two buckets, kept apart so a finding about code this PR never touched can't
    # masquerade as part of the verdict on the PR: `inscope` = findings on a
    # changed file that just couldn't be anchored to a hunk line; `offscope` =
    # findings on files outside the diff entirely.
    inscope: list[str] = []
    offscope: list[str] = []

    for f in findings:
        sev = f.get("severity", "Suggestion")
        title = f.get("title", "")
        fbody = f.get("body", "")
        comment = f"**{tag}** **{sev}** — {title}\n\n{fbody}"
        path = f.get("path")
        line = _as_int(f.get("line"))
        in_pr_file = bool(path) and path in ctx.diff_lines
        # Inline-postable only if the anchor lands on a line GitHub accepts: one
        # inside a changed hunk. Everything else routes to the summary directly
        # rather than firing a POST we know would 422; report runs route all there.
        anchorable = in_pr_file and line is not None and path is not None and line in ctx.diff_lines[path]
        if ctx.mode == "post" and anchorable:
            p = post_inline(ctx.slug, ctx.pr, ctx.head, f, comment, line)
            if p.returncode == 0:
                print(f"  [{lane}] inline {sev} @ {path}:{line}")
                continue
            # Anchor was in the diff yet the POST still failed (auth / API / race) —
            # surface gh's reason and fall back so the finding isn't lost.
            why = (p.stderr.strip().splitlines() or ["gh api error"])[-1]
            print(f"  [{lane}] inline post failed @ {path}:{line}: {why}", file=sys.stderr)
            ctx.inline_post_failed = True
            inscope.append(f"- **{sev}** — `{path}:{line}` (couldn't post inline: {why})\n\n  {fbody}")
            continue
        loc = (path or "") + (f":{line}" if line is not None else "")
        entry = f"- **{sev}** — `{loc}` {title}\n\n  {fbody}"
        (inscope if in_pr_file else offscope).append(entry)
        bucket = "inline" if in_pr_file else "OUT-OF-SCOPE"
        print(f"  [{lane}] summary ({bucket}) {sev} @ {loc or '(no anchor)'}")

    # Per-lane summary: assessment + strengths describe THIS PR's diff; the two
    # buckets render under distinct headers so out-of-scope findings read as
    # routing for a human, not as a verdict on this PR.
    head_line = f"**{tag}** — review summary" if ctx.mode == "post" else f"## {tag}"
    parts = [head_line, ""]
    if incomplete_reason:
        parts += [
            f"> ⚠ **Incomplete review** — {incomplete_reason}. Treat the verdict below with suspicion.",
            "",
        ]
    parts += [f"**Assessment:** {data.get('assessment', 'n/a')}", ""]
    if ctx.mode != "post":
        parts += ["**Behavioral delta:**", str(data.get("behavioral_delta", "n/a")), ""]
        inspected = data.get("inspected")
        if isinstance(inspected, list) and inspected:
            parts.append("**Inspected:**")
            for item in inspected:
                if not isinstance(item, dict):
                    continue
                symbols = ", ".join(item.get("symbols") or [])
                parts.append(f"- `{item.get('path', '')}` — {symbols}: {item.get('conclusion', '')}")
            parts.append("")
        raw_gaps = data.get("coverage_gaps")
        gaps = raw_gaps if isinstance(raw_gaps, list) else []
        parts.append("**Coverage gaps:**")
        parts += [f"- {gap}" for gap in gaps] if gaps else ["- None reported."]
        parts.append("")
        raw_change_map = data.get("change_map")
        change_map = raw_change_map if isinstance(raw_change_map, dict) else {}
        mermaid = change_map.get("mermaid")
        if isinstance(mermaid, str) and mermaid.strip():
            parts += ["**Change map:**", "", "```mermaid", mermaid.strip(), "```", ""]
    # The `method` (how-it-was-reviewed) section is proof-of-work for whoever reads
    # the review, not the PR author — show it in the report, keep posted comments lean.
    method = data.get("method")
    if method and ctx.mode != "post":
        parts += ["**How it was reviewed:**", method, ""]
    strengths = data.get("strengths") or []
    if strengths:
        parts.append("**Strengths:**")
        parts += [f"- {s}" for s in strengths]
        parts.append("")
    # PR-description feedback has no diff line to anchor to, so it only ever
    # lands here in the summary, never inline.
    notes = data.get("description_notes") or []
    if notes:
        parts.append("**PR description — tighten:**")
        parts += [f"- {n}" for n in notes]
        parts.append("")
    if inscope:
        header = (
            "**Findings not anchorable to a changed line:**" if ctx.mode == "post" else "**Findings (not posted):**"
        )
        parts += [header, "", *inscope, ""]
    if offscope:
        parts += [
            "**Out of scope — findings in files this PR did not change "
            "(surfaced for routing, not part of the verdict on this PR):**",
            "",
            *offscope,
        ]
    _post_or_print(lane, "summary comment", "\n".join(parts), ctx)


def _post_or_print(lane: str, what: str, body: str, ctx: ReviewCtx) -> None:
    if ctx.mode == "post":
        p = run(["gh", "pr", "comment", ctx.pr, "-R", ctx.slug, "--body-file", "-"], input=body)
        if p.returncode == 0:
            print(f"[{lane}] posted {what}")
        else:
            print(f"[{lane}] failed to post {what}: {p.stderr.strip()}", file=sys.stderr)
    else:
        # Don't-send mode: collect each lane's section for one consolidated report
        # (built and written in main) instead of dumping loose blocks to stdout.
        ctx.reports.append(body)
        print(f"[{lane}] captured {what} for report")


def post_review_overview(ctx: ReviewCtx) -> None:
    if ctx.mode != "post" or not ctx.review_overviews:
        return
    body = render_review_overview(ctx.review_overviews, ctx.head)
    p = run(["gh", "pr", "comment", ctx.pr, "-R", ctx.slug, "--body-file", "-"], input=body)
    if p.returncode == 0:
        print("posted consolidated review map")
    else:
        print(f"failed to post consolidated review map: {p.stderr.strip()}", file=sys.stderr)


def collect_review_overviews(lanes: list[str], results: dict[str, LaneResult], ctx: ReviewCtx) -> None:
    for lane in lanes:
        data = extract_findings(results[lane].out)
        if data is None or any(key not in data for key in REQUIRED_KEYS):
            continue
        enough_inspection = count_real_inspected_refs(data.get("inspected"), ctx.worktree) >= HOLLOW_INSPECTION_MIN_REFS
        if enough_inspection and not review_contract_issues(data, ctx.repo_files, changed_files=set(ctx.diff_lines)):
            ctx.review_overviews.append((lane, data))


def submit_pending_reviews_after_inline_failures(ctx: ReviewCtx) -> None:
    if ctx.mode != "post" or not ctx.inline_post_failed:
        return
    login = run(["gh", "api", "user", "--jq", ".login"])
    if login.returncode != 0:
        print(
            f"could not inspect pending reviews after inline post failure: {login.stderr.strip()}",
            file=sys.stderr,
        )
        return
    viewer = login.stdout.strip()
    reviews = run(
        [
            "gh",
            "api",
            f"repos/{ctx.slug}/pulls/{ctx.pr}/reviews",
            "--jq",
            f'.[] | select(.state == "PENDING" and .user.login == "{viewer}") | .id',
        ]
    )
    if reviews.returncode != 0:
        print(
            f"could not list pending reviews after inline post failure: {reviews.stderr.strip()}",
            file=sys.stderr,
        )
        return
    pending_ids = [line.strip() for line in reviews.stdout.splitlines() if line.strip()]
    if not pending_ids:
        print("no pending review to submit after inline post failure")
        return
    for review_id in pending_ids:
        submitted = run(
            [
                "gh",
                "api",
                "--method",
                "POST",
                f"repos/{ctx.slug}/pulls/{ctx.pr}/reviews/{review_id}/events",
                "-f",
                "event=COMMENT",
            ]
        )
        if submitted.returncode == 0:
            print(f"submitted pending review {review_id} after inline post failure")
        else:
            print(
                f"failed to submit pending review {review_id}: {submitted.stderr.strip()}",
                file=sys.stderr,
            )


def build_report(ctx: ReviewCtx, base_ref: Ref, base: Sha, lanes: list[str]) -> str:
    """Assemble the per-lane sections into one readable Markdown review."""
    head = [f"# Multi-model review — {ctx.slug} PR #{ctx.pr}"]
    if ctx.pr_title:
        head.append(f"**{ctx.pr_title}**")
    head += [
        "",
        f"`{base_ref}`@`{base[:12]}` … head `{ctx.head[:12]}` · lanes: {', '.join(lanes)} · threshold {THRESHOLD}",
        "",
    ]
    sections = ctx.reports or ["_No lane produced a review._"]
    report = "\n".join(head) + "\n" + "\n\n---\n\n".join(sections)
    if ctx.incomplete:
        report += "\n\n---\n\n> ⚠ incomplete reviews (missing required sections — verdicts discounted): " + "; ".join(
            ctx.incomplete
        )
    if ctx.failed:
        report += "\n\n---\n\n> ⚠ lanes that produced no review (crashed): " + ", ".join(ctx.failed)
    return report


# --- main --------------------------------------------------------------------
def main() -> None:
    # Python block-buffers stdout/stderr when they're redirected to a file
    # (background / headless runs), so progress stays invisible until the buffer
    # fills or the process exits. Force line buffering so each line streams live.
    # ty types sys.stdout/stderr as TextIO, which lacks reconfigure(); at runtime
    # they're TextIOWrapper, which has it.
    cast(TextIOWrapper, sys.stdout).reconfigure(line_buffering=True)
    cast(TextIOWrapper, sys.stderr).reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr", help="PR number")
    ap.add_argument("--base", help="override base branch")
    ap.add_argument(
        "--lanes",
        dest="lanes",
        default="claude,codex,antigravity",
        help="comma-separated harness lanes to run",
    )
    ap.add_argument("--models", dest="lanes", help="deprecated alias for --lanes")
    ap.add_argument("--post", action="store_true", help="force posting")
    ap.add_argument(
        "--report",
        nargs="?",
        const="",
        metavar="FILE",
        help="don't post; write the consolidated review to FILE "
        "(default: a path under the temp output dir). The "
        "post-nothing mode — use it to preview, or to review "
        "someone else's PR without touching it.",
    )
    ap.add_argument("--keep-worktree", action="store_true")
    ap.add_argument(
        "--review-kind",
        choices=list(render_review_prompt.REVIEW_KINDS),
        default=DEFAULT_REVIEW_KIND,
        help="built-in review prompt to render from the shared review-rubric skill (default: code)",
    )
    ap.add_argument(
        "--prompt",
        help="path to a custom prompt template to use instead of --review-kind "
        "(must keep the same {{...}} tokens + JSON output contract)",
    )
    ap.add_argument("--claude-model", help="override model for Claude Code")
    ap.add_argument(
        "--claude-fallback-model",
        help="override the Claude model used once when the primary model is unavailable",
    )
    ap.add_argument("--codex-model", help="override model for Codex")
    ap.add_argument("--antigravity-model", help="override model for Antigravity")
    ap.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh"],
        help="override reasoning effort for Claude and Codex",
    )
    args = ap.parse_args()

    if args.effort:
        LANE_EFFORTS["claude"] = args.effort
        LANE_EFFORTS["codex"] = args.effort

    if args.claude_model is not None:
        LANE_MODELS["claude"] = args.claude_model
    global CLAUDE_FALLBACK_MODEL
    if args.claude_fallback_model is not None:
        CLAUDE_FALLBACK_MODEL = args.claude_fallback_model
    if args.codex_model is not None:
        LANE_MODELS["codex"] = args.codex_model
    if args.antigravity_model is not None:
        LANE_MODELS["antigravity"] = args.antigravity_model
    LANE_EFFECTIVE_MODELS.update(LANE_MODELS)

    if args.post and args.report is not None:
        die("--post and --report are mutually exclusive")
    for tool in ("gh", "git"):
        if not shutil.which(tool):
            die(f"{tool} not found")
    prompt_path = Path(args.prompt).expanduser() if args.prompt else None

    pr = args.pr
    meta = json.loads(run_ok(["gh", "pr", "view", pr, "--json", "state,isDraft,baseRefName,headRefOid,body,title"]))
    repo = json.loads(run_ok(["gh", "repo", "view", "--json", "nameWithOwner,visibility"]))
    slug = repo["nameWithOwner"]
    visibility = repo["visibility"]  # PUBLIC | PRIVATE | INTERNAL
    head = Sha(meta["headRefOid"])
    base_ref = Ref(args.base or meta["baseRefName"])
    if not head:
        die(f"could not resolve PR #{pr} (is gh authenticated?)")

    mode: Mode
    if args.report is not None:
        mode = "report"
    elif args.post:
        mode = "post"
    else:
        mode = "report" if visibility == "PUBLIC" else "post"

    if meta["state"] != "OPEN":
        print(f"note: PR #{pr} is {meta['state']}.", file=sys.stderr)
    if meta["isDraft"]:
        print(f"note: PR #{pr} is a draft.", file=sys.stderr)

    requested = [lane.strip() for lane in args.lanes.split(",") if lane.strip()]
    for lane in requested:
        if lane not in LANES:
            print(f"unknown lane: {lane} — skipping", file=sys.stderr)
    lanes = []
    for lane in (lane for lane in requested if lane in LANES):
        binary = LANE_BINARIES.get(lane, lane)
        if shutil.which(binary):
            lanes.append(lane)
        else:
            hint = " (install the antigravity CLI 'agy')" if lane == "antigravity" else ""
            print(f"{lane} not installed — skipping{hint}", file=sys.stderr)
    if not lanes:
        die("no requested review harness CLIs are installed")

    # Resolve the base to the PR's actual fork point, then diff merge-base..HEAD —
    # exactly the PR's changes. The base tip we take the merge-base against must be
    # the commit GitHub computed the PR's diff against, NOT the live base-branch
    # tip: once a PR is merged, the live branch contains the PR's commits, so
    # merge-base(live tip, head) == head and the diff is empty. GitHub records and
    # freezes that base as `base.sha`, so prefer it; --base overrides; the live tip
    # is the last-resort fallback (and a stale *local* base would drag already-
    # merged commits in, which is why we always fetch fresh from origin).
    if (
        run(["git", "fetch", "--quiet", "origin", head]).returncode != 0
        and run(["git", "fetch", "--quiet", "origin", f"pull/{pr}/head"]).returncode != 0
    ):
        die(f"could not fetch PR head {head}")
    if args.base:
        if run(["git", "fetch", "--quiet", "origin", args.base]).returncode == 0:
            base_tip = run_ok(["git", "rev-parse", "FETCH_HEAD"]).strip()
        else:  # --base may be a local ref/sha (e.g. origin/main), not a branch on origin
            p = run(["git", "rev-parse", "--verify", "--quiet", args.base])
            if p.returncode != 0:
                die(f"could not resolve base ref {args.base}")
            base_tip = p.stdout.strip()
    else:
        live_tip = (
            run_ok(["git", "rev-parse", "FETCH_HEAD"]).strip()
            if run(["git", "fetch", "--quiet", "origin", base_ref]).returncode == 0
            else ""
        )
        recorded = run_ok(["gh", "api", f"repos/{slug}/pulls/{pr}", "--jq", ".base.sha"]).strip()
        if recorded and run(["git", "cat-file", "-e", recorded]).returncode != 0:
            run(["git", "fetch", "--quiet", "origin", recorded])  # bring it local if reachable
        if recorded and run(["git", "cat-file", "-e", recorded]).returncode == 0:
            base_tip = recorded
        elif live_tip:
            print(
                f"note: PR base.sha {recorded[:12] or '(none)'} unavailable locally; "
                "using live base tip (a merged-PR diff may be empty)",
                file=sys.stderr,
            )
            base_tip = live_tip
        else:
            die(f"could not resolve a base for PR #{pr}")
    base = Sha(run_ok(["git", "merge-base", base_tip, head]).strip())
    diff_lines = diff_commentable_lines(base, head)

    print(f"Reviewing PR #{pr}  (base={base_ref}@{base[:12]}  head={head[:12]}  repo={slug}  {visibility})")
    print(f"Lanes: {','.join(lanes)}   threshold={THRESHOLD}   mode={mode}   ({len(diff_lines)} changed files)")
    if mode == "post":
        print(f">>> will POST inline comments to PR #{pr}")

    template = load_prompt_template(prompt_path, args.review_kind)
    out = Path(tempfile.mkdtemp(prefix=f"pr-{pr}-out."))
    print(f"lane artifacts: {out}")
    wt = tempfile.mkdtemp(prefix=f"pr-{pr}-review.")

    # Review in a disposable detached worktree, so the PR's code is checked out
    # without touching the main working tree. That tree may be mid-edit, or have
    # a dev server / containers bound to it that would break if its branch
    # switched out from under them. The worktree is removed on exit.
    run_ok(["git", "worktree", "add", "--detach", wt, head])

    repo_files = set(run_ok(["git", "-C", wt, "ls-files"]).splitlines())
    repo_files.update(run_ok(["git", "ls-tree", "-r", "--name-only", base]).splitlines())
    ctx = ReviewCtx(
        mode=mode,
        slug=slug,
        pr=pr,
        head=head,
        diff_lines=diff_lines,
        pr_title=meta.get("title", ""),
        repo_files=repo_files,
        worktree=Path(wt),
    )
    try:
        prompts = {
            lane: render_prompt(template, base=base, slug=slug, head=head, pr=pr, body=meta.get("body", ""))
            for lane in lanes
        }
        # Run lanes in parallel; each harness explores the worktree independently.
        # The lanes capture each CLI's output in memory (no growing file to
        # watch), so a heartbeat reports per-lane elapsed time — enough to tell a
        # live-but-slow run from a hung one. It can't see *what* a lane is doing.
        results: dict[str, LaneResult] = {}
        start = {lane: time.monotonic() for lane in lanes}
        done: dict[str, float] = {}
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(HEARTBEAT_SECS):
                now = time.monotonic()
                active = [(lane, now - start[lane]) for lane in lanes if lane not in done]
                if active:
                    parts = ", ".join(f"{lane} ({int(s)}s)" for lane, s in active)
                    print(f"  … still running: {parts}")

        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(lanes)) as ex:
                futs = {ex.submit(LANES[lane], prompts[lane], wt, out): lane for lane in lanes}
                for fut in concurrent.futures.as_completed(futs):
                    lane = futs[fut]
                    done[lane] = time.monotonic()
                    try:
                        results[lane] = fut.result()
                    except Exception as e:  # noqa: BLE001 - surface, don't abort the batch
                        print(f"[{lane}] lane crashed: {e}", file=sys.stderr)
                        results[lane] = LaneResult("", 1, str(e))
                    print(f"[{lane}] finished in {int(done[lane] - start[lane])}s")
                    (out / f"{lane}.raw").write_text(results[lane].out)
        finally:
            stop.set()
            hb.join(timeout=1)

        print("\n===================== posting / results =====================")
        collect_review_overviews(lanes, results, ctx)
        post_review_overview(ctx)
        for lane in lanes:  # sequential posting: stable logs, no API races
            process_lane(lane, results[lane], ctx, out)
        submit_pending_reviews_after_inline_failures(ctx)
    finally:
        if args.keep_worktree:
            print(f"worktree kept at: {wt}", file=sys.stderr)
        else:
            run(["git", "worktree", "remove", "--force", wt])

    if ctx.incomplete:
        print(f"\n⚠ incomplete reviews (missing required sections): {'; '.join(ctx.incomplete)}", file=sys.stderr)
    if ctx.failed:
        print(f"\n⚠ lanes that failed (no review posted): {', '.join(ctx.failed)}", file=sys.stderr)

    if mode != "post":
        report = build_report(ctx, base_ref, base, lanes)
        report_path = Path(args.report) if args.report else (out / "review.md")
        report_path.write_text(report)
        print("\n" + report)
        print(f"\n(report mode — nothing posted. Review written to {report_path})")

    if ctx.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
