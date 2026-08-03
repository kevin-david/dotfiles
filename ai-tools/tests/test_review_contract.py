from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

multi_model_review = importlib.import_module("multi_model_review")


class ReviewContractTest(unittest.TestCase):
    def valid_review(self) -> dict[str, object]:
        return {
            "behavioral_delta": "Requests now pass through validation before persistence.",
            "inspected": [
                {
                    "path": "src/service.py",
                    "symbols": ["handle", "validate"],
                    "conclusion": "The caller and validation contract agree.",
                }
            ],
            "coverage_gaps": [],
            "change_map": {
                "components": [
                    {"name": "HTTP handler", "role": "Accepts the request"},
                    {"name": "Validator", "role": "Rejects invalid input"},
                    {"name": "Database", "role": "Persists accepted input"},
                ],
                "mermaid": "flowchart LR\n  H --> V\n  V --> D",
            },
        }

    def test_complete_contract_passes(self) -> None:
        issues = multi_model_review.review_contract_issues(
            self.valid_review(),
            {"src/service.py"},
        )

        self.assertEqual(issues, [])

    def test_default_prompts_require_review_evidence(self) -> None:
        for review_kind in ("code", "plan"):
            prompt = multi_model_review.load_prompt_template(None, review_kind)
            self.assertIn('"behavioral_delta"', prompt)
            self.assertIn('"inspected"', prompt)
            self.assertIn('"coverage_gaps"', prompt)
            self.assertIn('"change_map"', prompt)
            self.assertIn("`change_map.mermaid` is optional", prompt)
            sample = multi_model_review.extract_findings(prompt)
            self.assertIsNotNone(sample)
            self.assertFalse(set(multi_model_review.REQUIRED_KEYS) - set(sample or {}))

    def test_rendered_prompt_states_changed_root_coverage_contract(self) -> None:
        prompt = multi_model_review.render_prompt(
            "Review {{REPO_SLUG}} at {{HEAD_SHA}} against {{BASE_SHA}}. {{PR_BODY}}",
            base=multi_model_review.Sha("a" * 40),
            slug="owner/repo",
            head=multi_model_review.Sha("b" * 40),
            pr="1",
            body="Description",
        )

        self.assertIn("every changed top-level directory", prompt)
        self.assertIn("name that exact directory in `coverage_gaps`", prompt)

    def test_extract_findings_uses_complete_retry_after_abandoned_block(self) -> None:
        raw = """<<<REVIEW_JSON
{"assessment": "incomplete
retrying
<<<REVIEW_JSON
{"assessment": "complete", "findings": []}
REVIEW_JSON>>>
"""

        review = multi_model_review.extract_findings(raw)

        self.assertEqual(review, {"assessment": "complete", "findings": []})

    def test_empty_mermaid_is_valid_for_any_component_count(self) -> None:
        review = self.valid_review()
        review["change_map"]["mermaid"] = ""

        issues = multi_model_review.review_contract_issues(review, {"src/service.py"})

        self.assertEqual(issues, [])

    def test_inspection_evidence_must_name_real_paths_and_two_symbols(self) -> None:
        review = self.valid_review()
        review["inspected"] = [{"path": "missing.py", "symbols": ["one"], "conclusion": "Looked fine."}]

        issues = multi_model_review.review_contract_issues(review, {"src/service.py"})

        self.assertIn("inspected paths not in worktree: missing.py", issues)
        self.assertIn("inspected must name at least 2 verifiable file/symbol targets", issues)

    def test_changed_top_level_directories_need_inspection_or_an_explicit_gap(self) -> None:
        review = self.valid_review()
        changed_files = {"src/service.py", "dashboard/app.tsx", "pyproject.toml"}

        issues = multi_model_review.review_contract_issues(
            review,
            changed_files,
            changed_files=changed_files,
        )

        self.assertIn("changed top-level directories lack inspection or coverage gap: dashboard", issues)

        review["coverage_gaps"] = ["dashboard behavior was not inspected."]
        issues = multi_model_review.review_contract_issues(
            review,
            changed_files,
            changed_files=changed_files,
        )

        self.assertNotIn("changed top-level directories lack inspection or coverage gap: dashboard", issues)

    def test_overview_uses_one_map_and_combines_coverage_gaps(self) -> None:
        first = self.valid_review()
        first["coverage_gaps"] = ["Did not exercise the external API."]
        second = self.valid_review()
        second["coverage_gaps"] = ["Could not inspect production configuration."]

        overview = multi_model_review.render_review_overview(
            [("claude", first), ("codex", second)],
            multi_model_review.Sha("a" * 40),
        )

        self.assertEqual(overview.count("```mermaid"), 1)
        self.assertIn("Did not exercise the external API.", overview)
        self.assertIn("Could not inspect production configuration.", overview)
        self.assertIn("`aaaaaaaaaaaa`", overview)


if __name__ == "__main__":
    unittest.main()


class LeadLabelContractTest(unittest.TestCase):
    """partition_findings enforces the templates' lead-label rule at the
    structured-output boundary: an unlabelled confidence-50 finding must not
    survive to be rendered as an established claim."""

    def test_unlabelled_lead_is_dropped_and_reported(self) -> None:
        kept, dropped = multi_model_review.partition_findings(
            [{"confidence": 50, "title": "Regression in risk path", "body": "This is broken."}], 50
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, ["Regression in risk path"])

    def test_labelled_lead_is_kept(self) -> None:
        kept, dropped = multi_model_review.partition_findings(
            [{"confidence": 50, "title": "Possible stale read", "body": "Unverified: worker topology."}], 50
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_verified_finding_needs_no_label(self) -> None:
        kept, dropped = multi_model_review.partition_findings(
            [{"confidence": 90, "title": "Fail-open check", "body": "Proven by trace."}], 50
        )
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_subthreshold_findings_are_filtered_without_label_noise(self) -> None:
        kept, dropped = multi_model_review.partition_findings(
            [{"confidence": 25, "title": "Hunch", "body": "Maybe."}], 50
        )
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [])

    def test_missing_confidence_defaults_to_kept(self) -> None:
        kept, dropped = multi_model_review.partition_findings([{"title": "No confidence field", "body": "Body."}], 50)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])
