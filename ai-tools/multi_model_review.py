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
  - Public repos          -> dry run by default (opt in with --post).
  - Private/internal repos -> post automatically ("let it fly").
  - --post / --dry-run override the visibility default.

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
from collections import namedtuple
from pathlib import Path
from typing import NoReturn

# A lane's outcome: its final text plus the exit code / stderr of the CLI, so a
# crashed lane (bad auth, untrusted dir) is told apart from one that ran fine
# and found nothing. Without the code, both look like "no output".
LaneResult = namedtuple("LaneResult", "out code err")

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


def die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, capturing text output. Does not raise on nonzero."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def run_ok(cmd: list[str], **kw) -> str:
    """Run a command that must succeed; return stdout."""
    p = run(cmd, **kw)
    if p.returncode != 0:
        die(f"command failed: {' '.join(cmd)}\n{p.stderr.strip()}")
    return p.stdout


def tag_for(lane: str) -> str:
    return f"[{LABELS[lane]}]"


# --- prompt ------------------------------------------------------------------
def render_prompt(template: str, *, tag: str, base: str, slug: str, head: str, pr: str) -> str:
    # The reviewer label is injected (not asked for) so a model can't mislabel
    # itself in the posted tag.
    repl = {
        "{{REVIEWER_TAG}}": tag,
        "{{BASE_REF}}": base,
        "{{REPO_SLUG}}": slug,
        "{{HEAD_SHA}}": head,
        "{{PR_NUMBER}}": pr,
        "{{THRESHOLD}}": str(THRESHOLD),
    }
    for k, v in repl.items():
        template = template.replace(k, v)
    return template


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
    cmd = ["codex", "exec", "-s", "read-only", "-C", wt,
           "-c", f'model_reasoning_effort="{EFFORT}"',
           "--output-last-message", str(last)]
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
def extract_findings(raw: str) -> dict | None:
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
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def post_inline(slug: str, pr: str, head: str, f: dict, body: str, line: int) -> subprocess.CompletedProcess:
    """One review comment per finding => its own resolvable thread."""
    cmd = ["gh", "api", "--method", "POST", f"repos/{slug}/pulls/{pr}/comments",
           "-f", f"commit_id={head}",
           "-f", f"path={f['path']}",
           "-f", "side=RIGHT",
           "-F", f"line={line}",
           "-f", f"body={body}"]
    start = _as_int(f.get("start_line"))
    if start is not None:
        cmd += ["-f", "start_side=RIGHT", "-F", f"start_line={start}"]
    return run(cmd)


def process_lane(lane: str, res: LaneResult, ctx: dict, out: Path) -> None:
    tag = tag_for(lane)
    raw = res.out
    data = extract_findings(raw)

    # A lane that produced no findings AND either exited nonzero or emitted
    # nothing didn't "find nothing" — it crashed (bad auth, untrusted dir,
    # network). Surface it loudly and don't post error noise to the PR; a clean
    # no-findings run still emits a JSON block, so data would not be None here.
    if data is None and (res.code != 0 or not raw.strip()):
        tail = (res.err or raw).strip().splitlines()[-4:] or ["(no stderr captured)"]
        print(f"[{lane}] FAILED — exit {res.code}, no findings produced. Last stderr:",
              file=sys.stderr)
        for line in tail:
            print(f"    {line}", file=sys.stderr)
        ctx["failed"].append(lane)
        return

    if data is None:
        print(f"[{lane}] no valid JSON block; treating raw output as a summary")
        text = f"**{tag}** — automated review (unstructured output)\n\n{raw}"
        _post_or_print(lane, "raw summary", text, ctx)
        return

    findings = [f for f in data.get("findings", [])
                if f.get("confidence", 100) >= THRESHOLD]
    fallback: list[str] = []

    for f in findings:
        sev = f.get("severity", "Suggestion")
        title = f.get("title", "")
        fbody = f.get("body", "")
        comment = f"**{tag}** **{sev}** — {title}\n\n{fbody}"
        # A finding is inline-postable only if it names a path and a line we can
        # coerce to an int. A malformed anchor falls back to the summary rather
        # than crashing the whole (sequential) posting loop for later lanes.
        line = _as_int(f.get("line"))
        if ctx["mode"] == "post" and f.get("path") and line is not None:
            p = post_inline(ctx["slug"], ctx["pr"], ctx["head"], f, comment, line)
            if p.returncode == 0:
                print(f"  [{lane}] inline {sev} @ {f['path']}:{line}")
                continue
            # Often the line is outside the diff, but it could be an API/auth
            # error — surface gh's actual reason instead of assuming one cause.
            why = (p.stderr.strip().splitlines() or ["gh api error"])[-1]
            print(f"  [{lane}] inline post failed @ {f['path']}:{line}: {why}", file=sys.stderr)
            fallback.append(f"- **{sev}** — `{f['path']}:{line}` (couldn't post inline: {why})\n\n  {fbody}")
        else:
            loc = f.get("path", "") + (f":{f['line']}" if f.get("line") else "")
            fallback.append(f"- **{sev}** — `{loc}` {title}\n\n  {fbody}")

    # Per-model summary: assessment + strengths + anything not anchorable inline.
    parts = [f"**{tag}** — review summary", "",
             f"**Assessment:** {data.get('assessment', 'n/a')}", ""]
    strengths = data.get("strengths") or []
    if strengths:
        parts.append("**Strengths:**")
        parts += [f"- {s}" for s in strengths]
        parts.append("")
    if fallback:
        header = ("**Findings not anchorable to a changed line:**"
                  if ctx["mode"] == "post" else "**Findings (dry run — not posted):**")
        parts += [header, "", *fallback]
    _post_or_print(lane, "summary comment", "\n".join(parts), ctx)


def _post_or_print(lane: str, what: str, body: str, ctx: dict) -> None:
    if ctx["mode"] == "post":
        p = run(["gh", "pr", "comment", ctx["pr"], "-R", ctx["slug"], "--body-file", "-"], input=body)
        if p.returncode == 0:
            print(f"[{lane}] posted {what}")
        else:
            print(f"[{lane}] failed to post {what}: {p.stderr.strip()}", file=sys.stderr)
    else:
        print(f"----- {lane} {what} (dry run) -----\n{body}\n")


# --- main --------------------------------------------------------------------
def main() -> None:
    # Python block-buffers stdout/stderr when they're redirected to a file
    # (background / headless runs), so progress stays invisible until the buffer
    # fills or the process exits. Force line buffering so each line streams live.
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr", help="PR number")
    ap.add_argument("--base", help="override base branch")
    ap.add_argument("--models", default="claude,codex,gemini",
                    help="comma-separated lanes to run")
    ap.add_argument("--post", action="store_true", help="force posting")
    ap.add_argument("--dry-run", action="store_true", help="force preview, post nothing")
    ap.add_argument("--keep-worktree", action="store_true")
    args = ap.parse_args()

    if args.post and args.dry_run:
        die("--post and --dry-run are mutually exclusive")
    for tool in ("gh", "git"):
        if not shutil.which(tool):
            die(f"{tool} not found")
    if not PROMPT_TEMPLATE.exists():
        die(f"prompt template missing: {PROMPT_TEMPLATE}")

    pr = args.pr
    meta = json.loads(run_ok(["gh", "pr", "view", pr, "--json",
                              "state,isDraft,baseRefName,headRefOid"]))
    repo = json.loads(run_ok(["gh", "repo", "view", "--json", "nameWithOwner,visibility"]))
    slug = repo["nameWithOwner"]
    visibility = repo["visibility"]  # PUBLIC | PRIVATE | INTERNAL
    head = meta["headRefOid"]
    base = args.base or meta["baseRefName"]
    if not head:
        die(f"could not resolve PR #{pr} (is gh authenticated?)")

    if args.dry_run:
        mode = "dry"
    elif args.post:
        mode = "post"
    else:
        mode = "dry" if visibility == "PUBLIC" else "post"

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

    print(f"Reviewing PR #{pr}  (base={base}  head={head[:12]}  repo={slug}  {visibility})")
    print(f"Lanes: {','.join(lanes)}   effort={EFFORT}   threshold={THRESHOLD}   mode={mode}")
    if mode == "post":
        print(f">>> will POST inline comments to PR #{pr}")

    template = PROMPT_TEMPLATE.read_text()
    out = Path(tempfile.mkdtemp(prefix=f"pr-{pr}-out."))
    wt = tempfile.mkdtemp(prefix=f"pr-{pr}-review.")

    # Review in a disposable detached worktree, so the PR's code is checked out
    # without touching the main working tree. That tree may be mid-edit, or have
    # a dev server / containers bound to it that would break if its branch
    # switched out from under them. The worktree is removed on exit.
    if (run(["git", "fetch", "--quiet", "origin", head]).returncode != 0
            and run(["git", "fetch", "--quiet", "origin", f"pull/{pr}/head"]).returncode != 0):
        die(f"could not fetch PR head {head}")
    run_ok(["git", "worktree", "add", "--detach", wt, head])

    ctx = {"mode": mode, "slug": slug, "pr": pr, "head": head, "failed": []}
    try:
        prompts = {
            lane: render_prompt(template, tag=tag_for(lane), base=base,
                                slug=slug, head=head, pr=pr)
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

    if ctx["failed"]:
        print(f"\n⚠ lanes that failed (no review posted): {', '.join(ctx['failed'])}",
              file=sys.stderr)

    if mode != "post":
        print(f"\n(dry run — nothing posted. Outputs in {out})")

    if ctx["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
