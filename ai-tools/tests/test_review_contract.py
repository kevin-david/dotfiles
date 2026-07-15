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
            self.assertIn("at least three components", prompt)
            sample = multi_model_review.extract_findings(prompt)
            self.assertIsNotNone(sample)
            self.assertFalse(set(multi_model_review.REQUIRED_KEYS) - set(sample or {}))

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

    def test_three_component_map_requires_flowchart(self) -> None:
        review = self.valid_review()
        review["change_map"]["mermaid"] = ""

        issues = multi_model_review.review_contract_issues(review, {"src/service.py"})

        self.assertIn("change_map.mermaid required for 3+ components", issues)

    def test_inspection_evidence_must_name_real_paths_and_two_symbols(self) -> None:
        review = self.valid_review()
        review["inspected"] = [{"path": "missing.py", "symbols": ["one"], "conclusion": "Looked fine."}]

        issues = multi_model_review.review_contract_issues(review, {"src/service.py"})

        self.assertIn("inspected paths not in worktree: missing.py", issues)
        self.assertIn("inspected must name at least 2 verifiable file/symbol targets", issues)

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
