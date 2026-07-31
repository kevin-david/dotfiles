from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

multi_model_review = importlib.import_module("multi_model_review")
render_review_prompt = importlib.import_module("render_review_prompt")


class ReviewPromptTemplateDiscoveryTest(unittest.TestCase):
    def test_review_rubric_env_accepts_skill_root_and_templates_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "review-rubric"
            templates = root / "templates"
            templates.mkdir(parents=True)

            for configured_path in (root, templates):
                with mock.patch.dict("os.environ", {"REVIEW_RUBRIC_DIR": str(configured_path)}, clear=False):
                    self.assertEqual(render_review_prompt.templates_dir(), templates)


# Shared review concepts and the authored paths each must appear in. A marker
# is a presence check, not a quality check: it catches a concept disappearing
# from a path, not a shallow treatment surviving on one (the known blind spot —
# reconcile depth by review, not by prose-length assertions).
SHARED_CONCEPT_MARKERS = {
    "producing mechanism": ("manual", "code", "plan"),
    "subtractive": ("manual", "code", "plan"),
    "round trips": ("manual", "code", "plan"),
    "Unverified:": ("manual", "code", "plan"),
    "event loop stalls": ("manual", "code", "plan"),
    "tooltip": ("manual", "code", "plan"),
    "presumptively": ("manual", "code", "plan"),
    "risk ordered first": ("manual", "plan"),
}


class ReviewPromptRenderingTest(unittest.TestCase):
    def setUp(self) -> None:
        # These tests render from the external review-rubric skill. On a fresh
        # clone without the skill installed, skip rather than error opaquely.
        try:
            render_review_prompt.templates_dir()
        except FileNotFoundError:
            self.skipTest("review-rubric skill not installed locally")

    def _authored_paths(self) -> dict[str, str]:
        templates = render_review_prompt.templates_dir()
        return {
            "manual": (templates.parent / "manual.md").read_text(),
            "code": (templates / "code-review.md").read_text(),
            "plan": (templates / "plan-review.md").read_text(),
        }

    def test_shared_concepts_present_in_expected_authored_paths(self) -> None:
        texts = self._authored_paths()
        for marker, expected_paths in SHARED_CONCEPT_MARKERS.items():
            for key in expected_paths:
                self.assertIn(marker, texts[key], f"marker {marker!r} missing from the {key} path")

    def test_no_positional_lens_references(self) -> None:
        # Cross-references must use lens names, not numbers, so inserting a
        # lens can never silently re-point them.
        for key, text in self._authored_paths().items():
            self.assertNotRegex(text, r"(?i)lens \d", f"positional lens reference in the {key} path")

    def test_code_and_plan_prompts_include_shared_rubric(self) -> None:
        code_prompt = render_review_prompt.render_prompt("code")
        plan_prompt = render_review_prompt.render_prompt("plan")

        shared_markers = [
            "## Honor the repo's conventions",
            "## Verify before you report — adversarial confidence gate",
            "## Output — emit exactly one JSON block",
        ]
        for marker in shared_markers:
            self.assertIn(marker, code_prompt)
            self.assertIn(marker, plan_prompt)

        self.assertIn("## How to work — go deep", code_prompt)
        self.assertIn("## How to work — verify the plan against reality", plan_prompt)
        self.assertIn("You are reviewing the changes on the checked-out branch", code_prompt)
        self.assertIn("What's under review is a *plan*, not code", plan_prompt)
        self.assertNotIn("What's under review is a *plan*, not code", code_prompt)

    def test_prompts_require_validator_compatible_method_evidence(self) -> None:
        code_prompt = render_review_prompt.render_prompt("code")
        plan_prompt = render_review_prompt.render_prompt("plan")

        for prompt in (code_prompt, plan_prompt):
            self.assertIn("at least two backticked references", prompt)
            self.assertIn("Unquoted names do not count", prompt)

    def test_prompts_require_complete_contract_migration_sweeps(self) -> None:
        code_prompt = render_review_prompt.render_prompt("code")
        plan_prompt = render_review_prompt.render_prompt("plan")

        marker = "implementation, adapter, fake/mock, and type-checker escape hatch"
        code_prompt_normalized = " ".join(code_prompt.split())
        plan_prompt_normalized = " ".join(plan_prompt.split())
        self.assertIn(marker, code_prompt_normalized)
        self.assertIn(marker, plan_prompt_normalized)
        sweep_marker = 'A bare "checked fakes" does not satisfy this sweep'
        self.assertIn(sweep_marker, code_prompt_normalized)
        self.assertIn(sweep_marker, plan_prompt_normalized)

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
