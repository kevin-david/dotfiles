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
        self.assertEqual(multi_model_review.CLAUDE_LIMIT_RETRY_MODEL, "opus")
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

    def test_claude_retries_a_fable_limit_response_with_opus(self) -> None:
        limited = CompletedProcess(
            args=[],
            returncode=0,
            stdout="You've reached your Fable 5 limit. /model to switch models.",
            stderr="",
        )
        reviewed = CompletedProcess(args=[], returncode=0, stdout="review", stderr="")

        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            with (
                patch.dict(multi_model_review.LANE_MODELS, {"claude": "fable"}),
                patch.object(multi_model_review, "run", side_effect=[limited, reviewed]) as run,
            ):
                result = multi_model_review.lane_claude("prompt", td, out)
                commands = [call.args[0] for call in run.call_args_list]

        self.assertEqual(result, multi_model_review.LaneResult("review", 0, ""))
        self.assertEqual(commands[0][commands[0].index("--model") + 1], "fable")
        self.assertEqual(commands[1][commands[1].index("--model") + 1], "opus")

    def test_claude_limit_response_from_opus_fails_the_lane(self) -> None:
        limited = CompletedProcess(
            args=[],
            returncode=0,
            stdout="You have reached your Opus limit. /model to switch models.",
            stderr="",
        )

        with (
            tempfile.TemporaryDirectory() as td,
            patch.dict(multi_model_review.LANE_MODELS, {"claude": "opus"}),
            patch.object(multi_model_review, "run", return_value=limited),
        ):
            result = multi_model_review.lane_claude("prompt", td, Path(td))

        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertIn("Opus limit", result.err)


if __name__ == "__main__":
    unittest.main()
