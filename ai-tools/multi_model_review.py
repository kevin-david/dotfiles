#!/usr/bin/env python3
"""Fan a single PR out to multiple model reviewers (Claude Code, Codex, Gemini).

Each model runs headless against the same neutral prompt (review-prompt.md) and
emits structured findings; this runner posts them as INLINE review comments
anchored to the changed lines, so each finding becomes its own resolvable
thread on the PR. Cross-cutting findings are anchored once, at the model's
chosen best location.

Cross-harness by construction: orchestration lives here, not inside any one
CLI's skill/workflow system, so adding a model is one entry in LANES.

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
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from io import TextIOWrapper
from pathlib import Path
from subprocess import CompletedProcess
from typing import Literal, NamedTuple, NewType, NoReturn, TypedDict, cast

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
    """One model-emitted finding. ``total=False`` because this is the untrusted
    JSON seam: any key may be absent, which is exactly what the inline-vs-summary
    routing and ``_as_int`` guards already handle per-field."""

    path: str
    line: int
    start_line: int
    severity: Severity
    title: str
    confidence: float
    body: str


class LaneReview(TypedDict, total=False):
    """A lane's full parsed review block. ``total=False`` pairs with the
    ``REQUIRED_KEYS`` presence check: missing sections are detected and flagged,
    not assumed present."""

    eligible: bool
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
    failed: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    reports: list[str] = field(default_factory=list)


PROMPT_TEMPLATE = Path(__file__).resolve().parent / "review-prompt.md"
SENTINEL_OPEN = "<<<REVIEW_JSON"
SENTINEL_CLOSE = "REVIEW_JSON>>>"


def env(name: str, default: str) -> str:
    return os.environ.get(name, default)


EFFORT = env("REVIEW_EFFORT", "high")
THRESHOLD = int(env("REVIEW_THRESHOLD", "50"))
HEARTBEAT_SECS = int(env("REVIEW_HEARTBEAT_SECS", "30"))
LABELS = {
    "claude": env("REVIEW_CLAUDE_LABEL", "Claude Opus 4.8"),
    "codex": env("REVIEW_CODEX_LABEL", "Codex"),
    "gemini": env("REVIEW_GEMINI_LABEL", "Gemini"),
}
MODELS = {
    "claude": env("REVIEW_CLAUDE_MODEL", "opus"),
    "codex": env("REVIEW_CODEX_MODEL", ""),
    "gemini": env("REVIEW_GEMINI_MODEL", ""),
}


SEVERITY_RANK = {"Critical": 0, "Important": 1, "Suggestion": 2}
# Every section the prompt forces a reviewer to emit. A block missing any of
# these didn't do the work — the lane is flagged incomplete and its verdict
# discounted, rather than silently trusted as a clean pass.
REQUIRED_KEYS = ("eligible", "method", "assessment", "strengths", "description_notes", "findings")


def die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], **kw) -> CompletedProcess[str]:
    """Run a command, capturing text output. Does not raise on nonzero."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def run_ok(cmd: list[str], **kw) -> str:
    """Run a command that must succeed; return stdout."""
    p = run(cmd, **kw)
    if p.returncode != 0:
        die(f"command failed: {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout


def tag_for(lane: str) -> str:
    return f"[{LABELS[lane]}]"


# --- prompt ------------------------------------------------------------------
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
    return template.replace("{{PR_BODY}}", body.strip() or "(no description)")


# --- lanes -------------------------------------------------------------------
# Each lane runs a CLI headless in the worktree and returns its raw final text.
def lane_claude(prompt: str, wt: str, out: Path) -> LaneResult:
    model = MODELS["claude"]
    cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions"]
    if model:
        cmd += ["--model", model]
    p = run(cmd, cwd=wt)
    (out / "claude.err").write_text(p.stderr)
    return LaneResult(p.stdout, p.returncode, p.stderr)


def lane_codex(prompt: str, wt: str, out: Path) -> LaneResult:
    last = out / "codex.last"
    cmd = [
        "codex",
        "exec",
        "-s",
        "read-only",
        "-C",
        wt,
        "-c",
        f'model_reasoning_effort="{EFFORT}"',
        "--output-last-message",
        str(last),
    ]
    if MODELS["codex"]:
        cmd += ["-m", MODELS["codex"]]
    cmd += [prompt]
    p = run(cmd)
    (out / "codex.err").write_text(p.stderr)
    # codex writes its final message to `last`; stdout is the event log.
    text = last.read_text() if last.exists() else p.stdout
    return LaneResult(text, p.returncode, p.stderr)


def lane_gemini(prompt: str, wt: str, out: Path) -> LaneResult:
    # The review worktree is a fresh throwaway dir Gemini has never "trusted", so
    # it downgrades to default approval and refuses tool calls headlessly. --yolo
    # does not override trust; --skip-trust does.
    cmd = ["gemini", "-p", prompt, "--yolo", "--skip-trust"]
    if MODELS["gemini"]:
        cmd += ["-m", MODELS["gemini"]]
    p = run(cmd, cwd=wt)
    (out / "gemini.err").write_text(p.stderr)
    return LaneResult(p.stdout, p.returncode, p.stderr)


LANES = {"claude": lane_claude, "codex": lane_codex, "gemini": lane_gemini}


# --- findings ----------------------------------------------------------------
def extract_findings(raw: str) -> LaneReview | None:
    """Pull the JSON object between the sentinels. None if absent/invalid."""
    i = raw.find(SENTINEL_OPEN)
    if i == -1:
        return None
    i = raw.find("\n", i)
    j = raw.find(SENTINEL_CLOSE, i)
    if i == -1 or j == -1:
        return None
    try:
        return json.loads(raw[i:j])
    except json.JSONDecodeError:
        return None


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
    if missing:
        print(f"  [{lane}] INCOMPLETE — missing required section(s): {', '.join(missing)}", file=sys.stderr)
        ctx.incomplete.append(f"{lane} (missing: {', '.join(missing)})")

    findings = [f for f in data.get("findings", []) if f.get("confidence", 100) >= THRESHOLD]
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
            inscope.append(f"- **{sev}** — `{path}:{line}` (couldn't post inline: {why})\n\n  {fbody}")
            continue
        loc = (path or "") + (f":{line}" if line is not None else "")
        entry = f"- **{sev}** — `{loc}` {title}\n\n  {fbody}"
        (inscope if in_pr_file else offscope).append(entry)
        bucket = "inline" if in_pr_file else "OUT-OF-SCOPE"
        print(f"  [{lane}] summary ({bucket}) {sev} @ {loc or '(no anchor)'}")

    # Per-model summary: assessment + strengths describe THIS PR's diff; the two
    # buckets render under distinct headers so out-of-scope findings read as
    # routing for a human, not as a verdict on this PR.
    head_line = f"**{tag}** — review summary" if ctx.mode == "post" else f"## {tag}"
    parts = [head_line, ""]
    if missing:
        parts += [
            f"> ⚠ **Incomplete review** — missing section(s): {', '.join(missing)}. "
            "Treat the verdict below with suspicion.",
            "",
        ]
    parts += [f"**Assessment:** {data.get('assessment', 'n/a')}", ""]
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
    ap.add_argument("--models", default="claude,codex,gemini", help="comma-separated lanes to run")
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
    args = ap.parse_args()

    if args.post and args.report is not None:
        die("--post and --report are mutually exclusive")
    for tool in ("gh", "git"):
        if not shutil.which(tool):
            die(f"{tool} not found")
    if not PROMPT_TEMPLATE.exists():
        die(f"prompt template missing: {PROMPT_TEMPLATE}")

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

    requested = [m.strip() for m in args.models.split(",") if m.strip()]
    for m in requested:
        if m not in LANES:
            print(f"unknown lane: {m} — skipping", file=sys.stderr)
    lanes = []
    for m in (m for m in requested if m in LANES):
        if shutil.which(m):  # CLI binary name == lane name
            lanes.append(m)
        else:
            hint = " (install: npm i -g @google/gemini-cli)" if m == "gemini" else ""
            print(f"{m} not installed — skipping{hint}", file=sys.stderr)
    if not lanes:
        die("no requested model CLIs are installed")

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
    print(
        f"Lanes: {','.join(lanes)}   effort={EFFORT}   threshold={THRESHOLD}   mode={mode}   "
        f"({len(diff_lines)} changed files)"
    )
    if mode == "post":
        print(f">>> will POST inline comments to PR #{pr}")

    template = PROMPT_TEMPLATE.read_text()
    out = Path(tempfile.mkdtemp(prefix=f"pr-{pr}-out."))
    wt = tempfile.mkdtemp(prefix=f"pr-{pr}-review.")

    # Review in a disposable detached worktree, so the PR's code is checked out
    # without touching the main working tree. That tree may be mid-edit, or have
    # a dev server / containers bound to it that would break if its branch
    # switched out from under them. The worktree is removed on exit.
    run_ok(["git", "worktree", "add", "--detach", wt, head])

    ctx = ReviewCtx(mode=mode, slug=slug, pr=pr, head=head, diff_lines=diff_lines, pr_title=meta.get("title", ""))
    try:
        prompts = {
            lane: render_prompt(template, base=base, slug=slug, head=head, pr=pr, body=meta.get("body", ""))
            for lane in lanes
        }
        for lane in lanes:
            (out / f"{lane}.prompt").write_text(prompts[lane])

        # Run lanes in parallel; each model explores the worktree independently.
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
        for lane in lanes:  # sequential posting: stable logs, no API races
            process_lane(lane, results[lane], ctx, out)
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
