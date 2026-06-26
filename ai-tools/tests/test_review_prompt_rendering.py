from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

multi_model_review = importlib.import_module("multi_model_review")
render_review_prompt = importlib.import_module("render_review_prompt")


class ReviewPromptRenderingTest(unittest.TestCase):
    def test_code_and_plan_prompts_include_shared_rubric(self) -> None:
        code_prompt = render_review_prompt.render_prompt("code")
        plan_prompt = render_review_prompt.render_prompt("plan")

        shared_markers = [
            "## How to work - go deep",
            "## Honor the repo's conventions",
            "## Verify before you report - adversarial confidence gate",
            "## Output - emit exactly one JSON block",
        ]
        for marker in shared_markers:
            self.assertIn(marker, code_prompt)
            self.assertIn(marker, plan_prompt)

        self.assertIn("You are reviewing the changes on the checked-out branch", code_prompt)
        self.assertIn("What's under review is a *plan*, not code", plan_prompt)
        self.assertNotIn("What's under review is a *plan*, not code", code_prompt)

    def test_legacy_prompt_files_match_rendered_prompts(self) -> None:
        self.assertEqual(
            (TOOLS_DIR / "review-prompt.md").read_text(),
            render_review_prompt.render_prompt("code"),
        )
        self.assertEqual(
            (TOOLS_DIR / "review-prompt-plan.md").read_text(),
            render_review_prompt.render_prompt("plan"),
        )

    def test_multi_review_loads_review_kind_unless_custom_prompt_is_supplied(self) -> None:
        self.assertEqual(
            multi_model_review.load_prompt_template(None, "plan"),
            render_review_prompt.render_prompt("plan"),
        )

        with tempfile.TemporaryDirectory() as td:
            custom_prompt = Path(td) / "custom-prompt.md"
            custom_prompt.write_text("custom prompt")
            self.assertEqual(multi_model_review.load_prompt_template(custom_prompt, "code"), "custom prompt")


if __name__ == "__main__":
    unittest.main()
