from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
review = importlib.import_module("multi_model_review")
HEAD = "1" * 40


class PreparedReviewTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.payload = self.root / "findings.json"
        self.requests = self.root / "requests.jsonl"
        gh = self.root / "gh"
        gh.write_text(
            f"#!{sys.executable}\n"
            "import json,sys\nfrom pathlib import Path\n"
            f"records=Path({str(self.requests)!r})\n"
            "args=sys.argv[1:]\n"
            "if '--method' not in args:\n"
            " print(json.dumps({'head': {'sha': '9'*40}})); sys.exit(0)\n"
            "with records.open('a') as f: f.write(json.dumps(args)+'\\n')\n"
            "count=len(records.read_text().splitlines())\n"
            "if Path(__file__).with_name('reject').exists() and count == 1:\n"
            " print('gh: Validation Failed (HTTP 422)',file=sys.stderr); sys.exit(1)\n"
            "if Path(__file__).with_name('interrupt').exists() and count == 1:\n"
            " print('transport failure',file=sys.stderr); sys.exit(1)\n"
            "if Path(__file__).with_name('malformed').exists() and count == 1:\n"
            " print('[]'); sys.exit(0)\n"
            "if Path(__file__).with_name('reject_summary').exists() and any(a.endswith('/reviews') for a in args):\n"
            " print('gh: Forbidden (HTTP 403)',file=sys.stderr); sys.exit(1)\n"
            "failure=Path(__file__).with_name('fail')\n"
            "if failure.exists() and (not failure.read_text() "
            "or len(records.read_text().splitlines()) >= int(failure.read_text())):\n"
            " print('transport failure',file=sys.stderr); sys.exit(1)\n"
            "print(json.dumps({'id':7,'html_url':'https://github.com/owner/repo/pull/42#discussion_r7'}))\n"
        )
        gh.chmod(0o755)
        self.environment = patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.data = {
            "repo": "owner/repo",
            "pr": "42",
            "commit_id": HEAD,
            "body": "**[Example pro]** Unanchored design concern",
            "comments": [
                {"path": "src/store.py", "line": 12, "body": "**[Example pro]** P2 — First finding"},
                {"path": "src/store.py", "line": 14, "body": "**[Example pro]** P3 — Second finding"},
            ],
        }
        self.save()

    def save(self):
        self.payload.write_text(json.dumps(self.data))

    def test_posts_every_prepared_finding_with_original_revision_and_body(self):
        receipt = review.post_prepared_review(self.payload, "42")
        calls = [json.loads(line) for line in self.requests.read_text().splitlines()]
        self.assertEqual(len(calls), 3)
        for call, finding in zip(calls[:2], self.data["comments"], strict=True):
            self.assertIn(f"commit_id={HEAD}", call)
            self.assertIn(f"body={finding['body']}", call)
            self.assertIn(f"line={finding['line']}", call)
        self.assertIn("body=" + self.data["body"], calls[2])
        self.assertEqual(receipt["status"], "posted")
        self.assertEqual(len(receipt["urls"]), 3)
        self.assertEqual(receipt["current_head"], "9" * 40)

    def test_invalid_second_anchor_goes_in_review_body_without_losing_other_finding(self):
        self.data["comments"][1]["line"] = 0
        self.save()
        receipt = review.post_prepared_review(self.payload, "42")
        calls = [json.loads(line) for line in self.requests.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        summary = next(value for value in calls[1] if value.startswith("body="))
        self.assertIn(self.data["comments"][1]["body"], summary)
        self.assertIn("src/store.py:0", summary)
        self.assertEqual(receipt["status"], "posted")
        self.assertTrue(receipt["issues"])

    def test_rejected_inline_finding_is_recovered_in_summary(self):
        (self.root / "reject").touch()
        receipt = review.post_prepared_review(self.payload, "42")
        calls = [json.loads(line) for line in self.requests.read_text().splitlines()]
        self.assertEqual(len(calls), 3)
        self.assertIn("body=" + self.data["comments"][1]["body"], calls[1])
        summary = next(value for value in calls[2] if value.startswith("body="))
        self.assertIn(self.data["comments"][0]["body"], summary)
        self.assertEqual(receipt["undelivered_comments"], [])
        self.assertEqual(receipt["status"], "posted")
        self.assertTrue(receipt["issues"])

    def test_uncertain_inline_outcome_does_not_block_other_posts_or_duplicate_finding(self):
        (self.root / "interrupt").touch()
        receipt = review.post_prepared_review(self.payload, "42")
        calls = [json.loads(line) for line in self.requests.read_text().splitlines()]
        self.assertEqual(len(calls), 3)
        self.assertIn("body=" + self.data["comments"][1]["body"], calls[1])
        self.assertIn("body=" + self.data["body"], calls[2])
        self.assertEqual(receipt["undelivered_comments"], [self.data["comments"][0]])
        self.assertEqual(receipt["status"], "posting_uncertain")
        self.assertEqual(len(receipt["urls"]), 2)

    def test_wrong_pr_is_refused_before_any_post(self):
        with self.assertRaises(SystemExit):
            review.post_prepared_review(self.payload, "43")
        self.assertFalse(self.requests.exists())

    def test_failed_summary_preserves_body_and_successful_inline_receipts(self):
        (self.root / "reject_summary").touch()
        receipt = review.post_prepared_review(self.payload, "42")
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(receipt["unposted_body"], self.data["body"])
        self.assertEqual(receipt["undelivered_comments"], [])
        self.assertEqual(len(receipt["urls"]), 2)

    def test_malformed_confirmation_retains_finding_without_automatic_duplicate(self):
        (self.root / "malformed").touch()
        receipt = review.post_prepared_review(self.payload, "42")
        self.assertEqual(receipt["status"], "posting_uncertain")
        self.assertEqual(receipt["undelivered_comments"], [self.data["comments"][0]])
        self.assertEqual(len(receipt["urls"]), 2)

    def test_cli_posts_prepared_findings_without_any_review_cli(self):
        result = subprocess.run(
            [sys.executable, str(Path(review.__file__)), "42", "--post-findings", str(self.payload)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "posted")
        self.assertEqual(len(self.requests.read_text().splitlines()), 3)

    def test_cli_flags_incomplete_delivery_after_attempting_remaining_posts(self):
        (self.root / "interrupt").touch()
        result = subprocess.run(
            [sys.executable, str(Path(review.__file__)), "42", "--post-findings", str(self.payload)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "posting_uncertain")
        self.assertEqual(len(self.requests.read_text().splitlines()), 3)

    def test_partial_publication_preserves_every_unconfirmed_finding_and_attempts_summary(self):
        (self.root / "fail").write_text("2")
        review.post_prepared_review(self.payload, "42")
        recorded = json.loads(self.payload.with_suffix(".delivery.json").read_text())
        self.assertEqual(recorded["status"], "posting_uncertain")
        self.assertEqual(len(recorded["urls"]), 1)
        self.assertEqual(len(self.requests.read_text().splitlines()), 3)
        self.assertEqual(recorded["undelivered_comments"], [self.data["comments"][1]])
        self.assertEqual(recorded["unposted_body"], self.data["body"])

    def test_uncertain_attempt_is_recorded_and_cannot_be_replayed(self):
        (self.root / "fail").touch()
        review.post_prepared_review(self.payload, "42")
        recorded = json.loads(self.payload.with_suffix(".delivery.json").read_text())
        self.assertEqual(recorded["status"], "posting_uncertain")
        (self.root / "fail").unlink()
        with self.assertRaises(SystemExit):
            review.post_prepared_review(self.payload, "42")
        self.assertEqual(len(self.requests.read_text().splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
