from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

review = importlib.import_module("multi_model_review")

HEAD = "1" * 40
BASE = "2" * 40
FINDINGS = '<<<REVIEW_JSON\n{"eligible": true, "assessment": "mergeable", "findings": []}\nREVIEW_JSON>>>'


def artifacts(out: Path, *, pr: str = "42", lanes: tuple[str, ...] = ("codex",), head: str = HEAD) -> Path:
    (out / "run.json").write_text(
        json.dumps({"pr": pr, "slug": "owner/repo", "head": head, "base": BASE, "base_ref": "main"})
    )
    for lane in lanes:
        (out / f"{lane}.raw").write_text(FINDINGS)
        (out / f"{lane}.outcome.json").write_text(
            json.dumps({"exit_code": 0, "model": "recorded-model", "effort": "medium"})
        )
        (out / f"{lane}.err").write_text("a diagnostic\n")
    return out


class SavedLaneResultsTest(unittest.TestCase):
    def test_recorded_exit_status_model_and_effort_are_used_not_current_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = artifacts(Path(td))
            (out / "codex.outcome.json").write_text(
                json.dumps({"exit_code": 3, "model": "recorded-model", "effort": "medium"})
            )
            with (
                patch.dict(review.LANE_EFFECTIVE_MODELS, {"codex": "today-default"}),
                patch.dict(review.LANE_EFFORTS, {"codex": "high"}),
            ):
                results = review.saved_lane_results(out, ["codex"])
                self.assertEqual(results["codex"].out, FINDINGS)
                self.assertEqual(results["codex"].code, 3)
                self.assertEqual(results["codex"].err, "a diagnostic\n")
                self.assertEqual(review.tag_for("codex"), "[Codex (recorded-model / medium)]")

    def test_lane_without_a_recorded_outcome_is_skipped_not_assumed_clean(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = artifacts(Path(td), lanes=("codex", "claude"))
            (out / "claude.outcome.json").unlink()  # cancelled before it finished
            results = review.saved_lane_results(out, ["claude", "codex"])
            self.assertEqual(list(results), ["codex"])

    def test_lane_with_no_saved_findings_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = artifacts(Path(td))
            (out / "codex.raw").unlink()
            self.assertEqual(review.saved_lane_results(out, ["codex"]), {})


class PostSavedReviewTest(unittest.TestCase):
    def _gh_and_git(self, *, live_head: str = HEAD, slug: str = "owner/repo"):
        def run_ok(cmd: list[str]) -> str:
            if cmd[:3] == ["gh", "repo", "view"]:
                return json.dumps({"nameWithOwner": slug})
            if cmd[0] == "gh":
                return json.dumps({"headRefOid": live_head, "title": "recorded title"})
            if cmd[:2] == ["git", "worktree"]:
                return ""
            if "ls-files" in cmd:
                return "api/pricing.py\n"
            if "ls-tree" in cmd:
                return "api/pricing.py\n"
            raise AssertionError(f"unexpected command: {cmd}")

        return run_ok

    def test_saved_findings_are_posted_without_running_any_lane(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = artifacts(Path(td))
            lanes = {name: patch.object(review, f"lane_{name}") for name in ("claude", "codex", "antigravity")}
            with (
                patch.dict(review.LANE_EFFECTIVE_MODELS),
                patch.dict(review.LANE_EFFORTS),
                patch.object(review, "run_ok", side_effect=self._gh_and_git()),
                patch.object(review, "run", return_value=CompletedProcess([], 0, "", "")),
                patch.object(review, "diff_commentable_lines", return_value={"api/pricing.py": {12}}),
                patch.object(review, "process_lanes") as process_lanes,
                lanes["claude"] as claude_lane,
                lanes["codex"] as codex_lane,
                lanes["antigravity"] as antigravity_lane,
            ):
                review.post_saved_review(out, "42", ["claude", "codex", "antigravity"])

            posted_lanes, results, ctx, artifact_dir = process_lanes.call_args.args
            self.assertEqual(posted_lanes, ["codex"])
            self.assertEqual(results["codex"].out, FINDINGS)
            self.assertEqual(ctx.mode, "post")
            self.assertEqual(ctx.head, HEAD)
            self.assertEqual(ctx.pr_title, "recorded title")
            self.assertEqual(artifact_dir, out)
            for lane in (claude_lane, codex_lane, antigravity_lane):
                lane.assert_not_called()

    def test_moved_pr_head_refuses_to_post_stale_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = artifacts(Path(td))
            with (
                patch.object(review, "run_ok", side_effect=self._gh_and_git(live_head="9" * 40)),
                patch.object(review, "process_lanes") as process_lanes,
                self.assertRaises(SystemExit),
            ):
                review.post_saved_review(out, "42", ["codex"])
            process_lanes.assert_not_called()

    def test_artifacts_from_another_repository_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = artifacts(Path(td))
            with (
                patch.object(review, "run_ok", side_effect=self._gh_and_git(slug="owner/other-repo")),
                patch.object(review, "process_lanes") as process_lanes,
                self.assertRaises(SystemExit),
            ):
                review.post_saved_review(out, "42", ["codex"])
            process_lanes.assert_not_called()

    def test_artifacts_for_another_pr_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = artifacts(Path(td), pr="7")
            with patch.object(review, "process_lanes") as process_lanes, self.assertRaises(SystemExit):
                review.post_saved_review(out, "42", ["codex"])
            process_lanes.assert_not_called()

    def test_directory_without_a_recorded_run_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with patch.object(review, "process_lanes") as process_lanes, self.assertRaises(SystemExit):
                review.post_saved_review(Path(td), "42", ["codex"])
            process_lanes.assert_not_called()

    def test_no_finished_lane_is_refused_rather_than_posting_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = artifacts(Path(td))
            (out / "codex.outcome.json").unlink()
            with (
                patch.object(review, "run_ok", side_effect=self._gh_and_git()),
                patch.object(review, "process_lanes") as process_lanes,
                self.assertRaises(SystemExit),
            ):
                review.post_saved_review(out, "42", ["codex"])
            process_lanes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
