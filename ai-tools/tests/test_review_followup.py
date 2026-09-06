from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
review = importlib.import_module("multi_model_review")


class FollowupTest(unittest.TestCase):
    def test_claude_resume_keeps_id_and_never_falls_back(self):
        response = CompletedProcess([], 1, "", "model unavailable")
        with tempfile.TemporaryDirectory() as td, patch.object(review, "run", return_value=response) as run:
            result = review.lane_claude("follow up", td, Path(td), session_id="existing-id")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result.code, 1)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--resume") + 1], "existing-id")

    def test_claude_json_result_preserves_session_and_review(self):
        response = CompletedProcess([], 0, json.dumps({"session_id": "existing-id", "result": "review"}), "")
        with tempfile.TemporaryDirectory() as td, patch.object(review, "run", return_value=response):
            result = review.lane_claude("follow up", td, Path(td), session_id="existing-id")
            self.assertEqual(result.out, "review")
            self.assertEqual(review.session_id_from_output("claude", response.stdout), "existing-id")

    def test_codex_resume_uses_explicit_thread(self):
        response = CompletedProcess([], 0, '{"type":"thread.started","thread_id":"existing-id"}', "")
        with tempfile.TemporaryDirectory() as td, patch.object(review, "run", return_value=response) as run:
            review.lane_codex("follow up", td, Path(td), session_id="existing-id")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("resume") + 1], "existing-id")
            self.assertIn("--json", command)
            self.assertNotIn("--last", command)

    def test_native_session_formats_and_conflicting_ids(self):
        events = {
            "claude": {"session_id": "original"},
            "codex": {"type": "thread.started", "thread_id": "original"},
            "antigravity": {"event": "init", "conversation_id": "original"},
        }
        for lane, event in events.items():
            with self.subTest(lane=lane):
                raw = json.dumps(event)
                self.assertEqual(review.session_id_from_output(lane, raw), "original")
                with self.assertRaisesRegex(ValueError, "found 2"):
                    review.session_id_from_output(lane, raw + "\n" + raw.replace("original", "other"))

    def test_antigravity_resume_does_not_retry_a_generation_timeout(self):
        head = CompletedProcess([], 0, "a" * 40, "")
        response = CompletedProcess([], 1, '{"result":{"error":"model generation timed out"}}', "")
        with tempfile.TemporaryDirectory() as td, patch.object(review, "run", side_effect=[head, response]) as run:
            result = review.lane_antigravity("Clarify.", td, Path(td), session_id="original")
            self.assertEqual(run.call_count, 2)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--conversation") + 1], "original")
            self.assertNotEqual(result.code, 0)


class FollowupBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wt = self.root / "repo"
        self.wt.mkdir()
        self.out = self.root / "artifacts"
        self.out.mkdir()
        self.followup = importlib.import_module("review_followup")
        for values in (review.LANE_MODELS, review.LANE_EFFECTIVE_MODELS, review.LANE_EFFORTS):
            restore = patch.dict(values)
            restore.start()
            self.addCleanup(restore.stop)
        review.run_ok(["git", "init", "-q", str(self.wt)])
        (self.wt / "service.py").write_text(
            "def handle():\n    return validate()\n\ndef validate():\n    return True\n"
        )
        review.run_ok(["git", "-C", str(self.wt), "add", "."])
        review.run_ok(
            [
                "git",
                "-C",
                str(self.wt),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "fixture",
            ]
        )
        self.head = review.run_ok(["git", "-C", str(self.wt), "rev-parse", "HEAD"]).strip()
        self.review_data = {
            "eligible": True,
            "behavioral_delta": "Validates requests.",
            "inspected": [
                {"path": "service.py", "symbols": ["handle", "validate"], "conclusion": "Caller uses validation."}
            ],
            "coverage_gaps": [],
            "change_map": {"components": [{"name": "Service", "role": "Validates"}], "mermaid": ""},
            "method": "Traced service.py handle and validate.",
            "assessment": "Ready.",
            "strengths": [],
            "description_notes": [],
            "findings": [],
        }
        self.metadata = {
            "worktree": str(self.wt),
            "head": self.head,
            "base": self.head,
            "base_ref": self.head,
            "pr": "1",
            "slug": "test/repo",
        }
        (self.out / "run.json").write_text(json.dumps(self.metadata))
        (self.out / "finished").touch()
        (self.out / "claude.session.json").write_text(
            json.dumps({"session_id": "old-session", "model": "test-model", "effort": "high"})
        )
        (self.out / "claude.stdout").write_text(json.dumps({"session_id": "old-session", "result": "original"}))
        (self.out / "claude.raw").write_text("original review")
        (self.out / "claude.prompt").write_text("original contract")

    def resumed(self, prompt, wt, out, *, session_id):
        self.assertIn("Answer the follow-up request directly in assessment", prompt)
        self.assertIn("Perform any omitted inspections or checks now", prompt)
        self.assertEqual(session_id, "old-session")
        self.assertEqual(wt, str(self.wt))
        self.assertEqual(review.LANE_MODELS["claude"], "test-model")
        (out / "claude.stdout").write_text(json.dumps({"session_id": self.returned_id}))
        return review.LaneResult("<<<REVIEW_JSON\n" + json.dumps(self.review_data) + "\nREVIEW_JSON>>>", 0, "")

    def test_valid_resume_reports_without_github_writes_and_preserves_original(self):
        self.returned_id = "old-session"
        with patch.dict(review.LANES, {"claude": self.resumed}), patch.object(review, "post_inline") as post:
            output = self.followup.follow_up(self.out, "claude", "Clarify validation.")
            report = (output / "review.md").read_text()
            self.assertIn("pending independent evidence check", report)
            self.assertIn("instruction compliance is not", report)
            post.assert_not_called()
        self.assertEqual((self.out / "claude.raw").read_text(), "original review")
        self.assertEqual(
            (self.out / "claude.stdout").read_text(), json.dumps({"session_id": "old-session", "result": "original"})
        )

    def test_wrong_returned_session_rejected_before_report(self):
        self.returned_id = "new-session"
        with (
            patch.dict(review.LANES, {"claude": self.resumed}),
            self.assertRaisesRegex(ValueError, "different session"),
        ):
            self.followup.follow_up(self.out, "claude", "Clarify validation.")
        self.assertEqual(list(self.out.glob("followup-*/review.md")), [])

    def test_changed_or_dirty_checkout_rejected_before_model_call(self):
        for violation in ("dirty", "head"):
            with self.subTest(violation=violation), patch.dict(review.LANES, {"claude": self.resumed}):
                if violation == "dirty":
                    (self.wt / "untracked").touch()
                else:
                    (self.wt / "untracked").unlink()
                    self.metadata["head"] = "a" * 40
                    (self.out / "run.json").write_text(json.dumps(self.metadata))
                with self.assertRaisesRegex(ValueError, "dirty|changed"):
                    self.followup.follow_up(self.out, "claude", "Clarify.")
        self.assertEqual(list(self.out.glob("followup-*")), [])

    def test_trust_rejected_original_and_missing_identity_rejected(self):
        (self.out / "claude.raw").write_text("")
        with self.assertRaisesRegex(ValueError, "no usable review"):
            self.followup.follow_up(self.out, "claude", "Clarify.")
        (self.out / "claude.raw").write_text("original")
        (self.out / "claude.stdout").write_text("{}")
        with self.assertRaisesRegex(ValueError, "native session ID"):
            self.followup.follow_up(self.out, "claude", "Clarify.")

    def test_completed_lane_needs_no_batch_marker_and_locks_only_its_session(self):
        import fcntl

        self.returned_id = "old-session"
        (self.out / "finished").unlink()
        with (self.out / "codex.followup.lock").open("w") as other:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.dict(review.LANES, {"claude": self.resumed}):
                output = self.followup.follow_up(self.out, "claude", "Clarify.")
                self.assertTrue((output / "review.md").exists())
        with (self.out / "claude.followup.lock").open("w") as same:
            fcntl.flock(same, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "already running"):
                self.followup.follow_up(self.out, "claude", "Clarify.")

    def test_incomplete_replacement_does_not_become_clean(self):
        self.returned_id = "old-session"
        del self.review_data["behavioral_delta"]
        with patch.dict(review.LANES, {"claude": self.resumed}), self.assertRaisesRegex(ValueError, "incomplete"):
            self.followup.follow_up(self.out, "claude", "Clarify.")


if __name__ == "__main__":
    unittest.main()
