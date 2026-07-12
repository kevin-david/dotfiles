from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

multi_model_review = importlib.import_module("multi_model_review")


class LaneConfigurationTest(unittest.TestCase):
    def test_default_reviewer_presets_keep_effort_with_its_harness(self) -> None:
        self.assertEqual(multi_model_review.LANE_MODELS["claude"], "fable")
        self.assertEqual(multi_model_review.LANE_EFFORTS["claude"], "high")
        self.assertEqual(multi_model_review.LANE_MODELS["codex"], "gpt-5.6-sol")
        self.assertEqual(multi_model_review.LANE_EFFORTS["codex"], "high")

    def test_claude_and_codex_commands_use_their_default_presets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            completed = CompletedProcess(args=[], returncode=0, stdout="review", stderr="")
            with patch.object(multi_model_review, "run", return_value=completed) as run:
                multi_model_review.lane_claude("prompt", td, out)
                claude_cmd = run.call_args.args[0]

                multi_model_review.lane_codex("prompt", td, out)
                codex_cmd = run.call_args.args[0]

        self.assertEqual(
            claude_cmd,
            [
                "claude",
                "-p",
                "prompt",
                "--permission-mode",
                "bypassPermissions",
                "--effort",
                "high",
                "--model",
                "fable",
            ],
        )
        self.assertIn('model_reasoning_effort="high"', codex_cmd)
        self.assertEqual(codex_cmd[codex_cmd.index("-m") + 1], "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()
