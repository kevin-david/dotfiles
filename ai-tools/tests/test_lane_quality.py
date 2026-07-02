"""Mechanical hollow-method check: a lane's method section must name real code."""

import json
import subprocess
from pathlib import Path

import multi_model_review as mmr


def make_worktree(tmp_path: Path) -> Path:
    wt = tmp_path / "wt"
    (wt / "svc").mkdir(parents=True)
    (wt / "svc" / "thing.py").write_text("def frobnicate():\n    return 1\n")
    for cmd in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=wt, check=True, capture_output=True)
    return wt


def make_ctx(wt: Path) -> mmr.ReviewCtx:
    return mmr.ReviewCtx(
        mode="report",
        slug="o/r",
        pr="1",
        head=mmr.Sha("deadbeef"),
        diff_lines={},
        pr_title="t",
        worktree=wt,
        repo_files={"svc/thing.py"},
    )


def lane_output(method: str | None) -> str:
    # Every section other than `method` is contract-valid, so only the
    # hollow-method dimension varies across these tests.
    data: dict = {
        "eligible": True,
        "behavioral_delta": "frobnicate now returns 1",
        "inspected": [
            {"path": "svc/thing.py", "symbols": ["frobnicate", "thing"], "conclusion": "traced"},
        ],
        "coverage_gaps": [],
        "change_map": {
            "components": [{"name": "svc", "role": "service"}],
            "mermaid": "",
        },
        "assessment": "ok",
        "strengths": [],
        "description_notes": [],
        "findings": [],
    }
    if method is not None:
        data["method"] = method
    return f"{mmr.SENTINEL_OPEN}\n{json.dumps(data)}\n{mmr.SENTINEL_CLOSE}"


def test_concrete_method_passes(tmp_path):
    wt = make_worktree(tmp_path)
    assert mmr.count_real_refs("I traced `svc/thing.py` and `frobnicate()` callers.", wt) == 2
    ctx = make_ctx(wt)
    res = mmr.LaneResult(lane_output("Traced `svc/thing.py` and `frobnicate()` down-stack."), 0, "")
    mmr.process_lane("claude", res, ctx, tmp_path)
    assert ctx.incomplete == []


def test_hollow_method_marked_incomplete(tmp_path):
    wt = make_worktree(tmp_path)
    ctx = make_ctx(wt)
    res = mmr.LaneResult(lane_output("Read the diff carefully and checked for bugs."), 0, "")
    mmr.process_lane("claude", res, ctx, tmp_path)
    assert len(ctx.incomplete) == 1
    assert "hollow method" in ctx.incomplete[0]


def test_symbol_must_exist_to_count(tmp_path):
    wt = make_worktree(tmp_path)
    assert mmr.count_real_refs("Checked `totally_fake_symbol_xyz` in `not/a/file.py`.", wt) == 0


def test_missing_method_still_incomplete(tmp_path):
    wt = make_worktree(tmp_path)
    ctx = make_ctx(wt)
    res = mmr.LaneResult(lane_output(None), 0, "")
    mmr.process_lane("claude", res, ctx, tmp_path)
    # Flagged once by the presence check, not double-flagged as hollow too.
    assert len(ctx.incomplete) == 1
    assert "missing" in ctx.incomplete[0] and "method" in ctx.incomplete[0]
