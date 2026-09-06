#!/usr/bin/env python3
"""Send one report-only follow-up to a recorded review session at its original snapshot."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import tempfile
from pathlib import Path

import multi_model_review as review


def verify_checkout(worktree: Path, head: str, base: str) -> None:
    actual = review.run_ok(["git", "-C", str(worktree), "rev-parse", "HEAD"]).strip()
    if actual != head:
        raise ValueError(f"review checkout changed: expected {head}, got {actual}")
    if review.run_ok(["git", "-C", str(worktree), "status", "--porcelain", "--untracked-files=all"]).strip():
        raise ValueError("review checkout is dirty; preserve it and stop")
    review.run_ok(["git", "-C", str(worktree), "cat-file", "-e", f"{base}^{{commit}}"])


def follow_up(artifacts: Path, lane: str, prompt: str) -> Path:
    artifacts = artifacts.resolve()
    if not (artifacts / f"{lane}.raw").read_text().strip():
        raise ValueError("original lane has no usable review; start a fresh review instead of resuming it")
    metadata = json.loads((artifacts / "run.json").read_text())
    session = json.loads((artifacts / f"{lane}.session.json").read_text())
    native_name = "antigravity.stream.jsonl" if lane == "antigravity" else f"{lane}.stdout"
    identity = review.session_id_from_output(lane, (artifacts / native_name).read_text())
    if session["session_id"] != identity:
        raise ValueError("recorded session ID disagrees with original native output")
    worktree = Path(metadata["worktree"])
    verify_checkout(worktree, metadata["head"], metadata["base"])
    if lane == "codex":
        current_home = str(Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser().resolve())
        if current_home != metadata["codex_home"]:
            raise ValueError(f"use the original CODEX_HOME: {metadata['codex_home']}")
    review.LANE_MODELS[lane] = session["model"]
    review.LANE_EFFECTIVE_MODELS[lane] = session["model"]
    if session["effort"] is not None:
        review.LANE_EFFORTS[lane] = session["effort"]

    with (artifacts / f"{lane}.followup.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(f"{lane}: another follow-up is already running") from error
        out = Path(tempfile.mkdtemp(prefix=f"followup-{lane}.", dir=artifacts))
        print(f"follow-up artifacts: {out}", flush=True)
        (out / "request.txt").write_text(prompt)
        (out / "session.json").write_text(json.dumps(session, indent=2))
        instructions = f"""Continue this existing review conversation at the ORIGINAL snapshot.
Head: {metadata["head"]}; diff base: {metadata["base"]}; checkout: {worktree}.
Do not inspect a newer PR head, edit repository files, or write to GitHub.
Treat this as an adversarial check of your previous review, not a formatting repair.
Identify the original instructions or conclusions challenged by the request.
Perform any omitted inspections or checks now; try to disprove your prior findings.
Use inspected and method to name the concrete targets, counterexamples, commands
and observed results supporting your answer. Distinguish newly performed work
from prior evidence. If you cannot perform a required check, report that gap
instead of asserting compliance or inventing evidence.
Apply this follow-up request:
{prompt}

Return a complete replacement review using the original required JSON contract,
including unchanged sections. Answer the follow-up request directly in assessment,
and explain any withdrawn or changed findings there.
The original contract remains available at {artifacts / (lane + ".prompt")}.
"""
        result = review.LANES[lane](instructions, str(worktree), out, session_id=identity)
        (out / f"{lane}.raw").write_text(result.out)
        verify_checkout(worktree, metadata["head"], metadata["base"])
        resumed = review.session_id_from_output(lane, (out / native_name).read_text())
        if resumed != identity:
            raise ValueError(f"CLI resumed a different session: {resumed}; output is not a usable review")
        # Existing report validation uses git relative to cwd.
        previous_cwd = Path.cwd()
        try:
            os.chdir(worktree)
            diff = review.diff_commentable_lines(review.Sha(metadata["base"]), review.Sha(metadata["head"]))
            files = set(review.run_ok(["git", "ls-files"]).splitlines())
            files.update(review.run_ok(["git", "ls-tree", "-r", "--name-only", metadata["base"]]).splitlines())
            ctx = review.ReviewCtx(
                mode="report",
                slug=metadata["slug"],
                pr=metadata["pr"],
                head=review.Sha(metadata["head"]),
                diff_lines=diff,
                pr_title="",
                repo_files=files,
                worktree=worktree,
            )
            data = review.extract_findings(result.out)
            if data is None:
                raise ValueError("follow-up returned no structured review; inspect preserved raw output")
            review.collect_review_overviews([lane], {lane: result}, ctx)
            review.process_lane(lane, result, ctx, out)
            (out / "review.md").write_text(
                "Follow-up pending independent evidence check. Structure and session identity "
                "are validated; instruction compliance is not.\n\n"
                + review.build_report(
                    ctx,
                    review.Ref(metadata["base_ref"]),
                    review.Sha(metadata["base"]),
                    [lane],
                )
            )
            if result.code or ctx.failed or ctx.incomplete:
                raise ValueError(f"follow-up failed or incomplete; inspect {out}")
        finally:
            os.chdir(previous_cwd)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", type=Path, help="original runner artifact directory")
    parser.add_argument("--lane", required=True, choices=list(review.LANES))
    parser.add_argument("--prompt", required=True, type=Path, help="follow-up request text file")
    args = parser.parse_args()
    try:
        out = follow_up(args.artifacts, args.lane, args.prompt.read_text())
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"follow-up stopped: {error}\n")
    print(f"Report: {out / 'review.md'} (nothing posted; independent evidence check required)")


if __name__ == "__main__":
    main()
