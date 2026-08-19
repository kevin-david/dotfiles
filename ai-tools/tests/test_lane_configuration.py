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

multi_model_review = importlib.import_module("multi_model_review")


class LaneConfigurationTest(unittest.TestCase):
    def test_default_reviewer_presets_keep_effort_with_its_harness(self) -> None:
        self.assertEqual(multi_model_review.LANE_MODELS["claude"], "fable")
        self.assertEqual(multi_model_review.CLAUDE_FALLBACK_MODEL, "opus")
        self.assertEqual(multi_model_review.LANE_EFFORTS["claude"], "high")
        self.assertEqual(multi_model_review.LANE_MODELS["codex"], "gpt-5.6-sol")
        self.assertEqual(multi_model_review.LANE_EFFORTS["codex"], "high")

    def test_claude_and_codex_commands_use_their_default_presets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            prompt = "review contract marker"
            completed = CompletedProcess(args=[], returncode=0, stdout="review", stderr="")
            with patch.object(multi_model_review, "run", return_value=completed) as run:
                multi_model_review.lane_claude(prompt, td, out)
                claude_cmd = run.call_args.args[0]

                multi_model_review.lane_codex(prompt, td, out)
                codex_cmd = run.call_args.args[0]

            self.assertEqual((out / "claude.prompt").read_text(), prompt)
            self.assertEqual((out / "codex.prompt").read_text(), prompt)

        self.assertEqual(claude_cmd[0:2], ["claude", "-p"])
        self.assertNotIn(prompt, claude_cmd)
        self.assertIn("claude.prompt", claude_cmd[2])
        self.assertEqual(claude_cmd[3:7], ["--permission-mode", "bypassPermissions", "--effort", "high"])
        self.assertEqual(claude_cmd[-2:], ["--model", "fable"])
        self.assertNotIn(prompt, codex_cmd)
        self.assertIn("codex.prompt", codex_cmd[-1])
        self.assertIn('model_reasoning_effort="high"', codex_cmd)
        self.assertEqual(codex_cmd[codex_cmd.index("-m") + 1], "gpt-5.6-sol")

    def test_claude_retries_once_with_opus_when_primary_is_unavailable(self) -> None:
        unavailable = CompletedProcess(
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
                patch.dict(multi_model_review.LANE_EFFECTIVE_MODELS, {"claude": "fable"}),
                patch.object(multi_model_review, "run", side_effect=[unavailable, reviewed]) as run,
            ):
                result = multi_model_review.lane_claude("prompt", td, out)
                commands = [call.args[0] for call in run.call_args_list]
                effective_tag = multi_model_review.tag_for("claude")

        self.assertEqual(result, multi_model_review.LaneResult("review", 0, ""))
        self.assertEqual(commands[0][commands[0].index("--model") + 1], "fable")
        self.assertEqual(commands[1][commands[1].index("--model") + 1], "opus")
        self.assertEqual(effective_tag, "[Claude (opus / high)]")

    def test_claude_fails_without_postable_output_when_fallback_is_unavailable(self) -> None:
        unavailable = CompletedProcess(
            args=[],
            returncode=0,
            stdout="You've reached your model limit. /model to switch models.",
            stderr="",
        )

        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(
                multi_model_review,
                "run",
                side_effect=(unavailable, unavailable),
            ),
        ):
            result = multi_model_review.lane_claude("prompt", td, Path(td))

        self.assertEqual(result.code, 1)
        self.assertEqual(result.out, "")
        self.assertIn("model limit", result.err)

    def test_claude_does_not_retry_an_unrelated_failure(self) -> None:
        auth_failure = CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr="authentication failed",
        )

        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(
                multi_model_review,
                "run",
                return_value=auth_failure,
            ) as run,
        ):
            result = multi_model_review.lane_claude("prompt", td, Path(td))

        self.assertEqual(result, multi_model_review.LaneResult("", 1, "authentication failed"))
        self.assertEqual(run.call_count, 1)

    def test_claude_does_not_retry_a_structured_review_that_mentions_a_limit(self) -> None:
        review = CompletedProcess(
            args=[],
            returncode=0,
            stdout=('<<<REVIEW_JSON\n{"assessment": "The API reached your configured limit."}\nREVIEW_JSON>>>'),
            stderr="",
        )

        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(
                multi_model_review,
                "run",
                return_value=review,
            ) as run,
        ):
            result = multi_model_review.lane_claude("prompt", td, Path(td))

        self.assertEqual(result.out, review.stdout)
        self.assertEqual(run.call_count, 1)

    def test_antigravity_returns_only_grounded_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            expected_head = "a" * 40
            provenance_cmd = f"git -C {worktree} rev-parse HEAD"
            review = {
                "eligible": True,
                "behavioral_delta": "Changes behavior.",
                "inspected": [{"path": "src/service.py", "symbols": ["run"], "conclusion": "Correct."}],
                "coverage_gaps": [],
                "change_map": {"components": [], "mermaid": ""},
                "method": "Inspected `src/service.py` and `run`.",
                "assessment": "ready",
                "strengths": [],
                "description_notes": [],
                "findings": [],
            }
            stream = "\n".join(
                [
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {
                                "state": "DONE",
                                "tool_info": {
                                    "name": "run_command",
                                    "parameters": {"CommandLine": provenance_cmd},
                                    "output": f"{expected_head}\n",
                                },
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {
                                "state": "DONE",
                                "tool_info": {
                                    "name": "view_file",
                                    "parameters": {"AbsolutePath": str(worktree / "src" / "service.py")},
                                },
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "status": "SUCCESS",
                                "response": f"{json.dumps(review)}\n{json.dumps(review)}",
                                "structured_output": review,
                            },
                        }
                    ),
                ]
            )
            completed = CompletedProcess(args=[], returncode=0, stdout=stream, stderr="")
            head = CompletedProcess(args=[], returncode=0, stdout=f"{expected_head}\n", stderr="")
            with (
                patch.object(multi_model_review, "run", side_effect=[head, completed]) as run,
                patch.dict(multi_model_review.LANE_MODELS, {"antigravity": ""}),
            ):
                result = multi_model_review.lane_antigravity("prompt", td, worktree)
                agy_cmd = run.call_args_list[1].args[0]

        self.assertIn(multi_model_review.SENTINEL_OPEN, result.out)
        self.assertIn(json.dumps(review), result.out)
        self.assertEqual(result.out.count('"eligible": true'), 1)
        self.assertEqual(result.code, 0)
        self.assertEqual(result.err, "")
        self.assertIn("--output-format", agy_cmd)
        self.assertIn("--json-schema", agy_cmd)
        grounded_prompt = agy_cmd[agy_cmd.index("-p") + 1]
        self.assertIn(str(worktree), grounded_prompt)
        self.assertIn(expected_head, grounded_prompt)

    def test_antigravity_keeps_checkpoint_recovery_in_the_prompt_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            worktree = root / "review"
            worktree.mkdir()
            out = root / "out"
            out.mkdir()
            expected_head = "a" * 40
            provenance_cmd = f"git -C {worktree} rev-parse HEAD"
            review = {
                "eligible": True,
                "behavioral_delta": "Changes behavior.",
                "inspected": [],
                "coverage_gaps": ["Not inspected."],
                "change_map": {"components": [], "mermaid": ""},
                "method": "Inspected the diff.",
                "assessment": "incomplete",
                "strengths": [],
                "description_notes": [],
                "findings": [],
            }
            stream = "\n".join(
                [
                    json.dumps(
                        {
                            "event": "step_update",
                            "step_update": {
                                "state": "DONE",
                                "tool_info": {
                                    "name": "run_command",
                                    "parameters": {"CommandLine": provenance_cmd},
                                    "output": expected_head,
                                },
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "status": "SUCCESS",
                                "structured_output": review,
                            },
                        }
                    ),
                ]
            )
            calls = 0

            def run(cmd: list[str], **_kwargs: object) -> CompletedProcess[str]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return CompletedProcess(args=cmd, returncode=0, stdout=f"{expected_head}\n", stderr="")

                instruction_path = out / "antigravity.prompt"
                instructions = instruction_path.read_text()
                self.assertIn("checkpoint recovery contract marker", instructions)
                self.assertIn(str(instruction_path), cmd[cmd.index("-p") + 1])
                self.assertNotIn("checkpoint recovery contract marker", cmd[cmd.index("-p") + 1])
                self.assertIn("transcript", instructions.lower())
                self.assertIn("For every changed", instructions)
                self.assertIn("top-level directory", instructions)
                schema_path = Path(cmd[cmd.index("--json-schema") + 1])
                self.assertEqual(json.loads(schema_path.read_text()), multi_model_review.ANTIGRAVITY_REVIEW_SCHEMA)
                return CompletedProcess(args=cmd, returncode=0, stdout=stream, stderr="")

            with (
                patch.object(multi_model_review, "run", side_effect=run),
                patch.dict(multi_model_review.LANE_MODELS, {"antigravity": ""}),
            ):
                result = multi_model_review.lane_antigravity("checkpoint recovery contract marker", str(worktree), out)

            self.assertEqual(result.code, 0)
            self.assertTrue((out / "antigravity.prompt").exists())
            self.assertTrue((out / "antigravity.schema.json").exists())

    def test_antigravity_retries_a_structured_generation_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            worktree = Path(td)
            expected_head = "a" * 40
            provenance_cmd = f"git -C {worktree} rev-parse HEAD"
            timed_out = CompletedProcess(
                args=[],
                returncode=1,
                stdout=json.dumps(
                    {
                        "event": "result",
                        "result": {
                            "status": "ERROR",
                            "response": "",
                            "error": "timeout waiting for response",
                        },
                    }
                ),
                stderr="",
            )
            reviewed = CompletedProcess(
                args=[],
                returncode=0,
                stdout="\n".join(
                    [
                        json.dumps(
                            {
                                "event": "step_update",
                                "step_update": {
                                    "state": "DONE",
                                    "tool_info": {
                                        "name": "run_command",
                                        "parameters": {"CommandLine": provenance_cmd},
                                        "output": expected_head,
                                    },
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "event": "result",
                                "result": {
                                    "status": "SUCCESS",
                                    "response": json.dumps(
                                        {
                                            "eligible": True,
                                            "behavioral_delta": "Changes behavior.",
                                            "inspected": [],
                                            "coverage_gaps": [],
                                            "change_map": {"components": [], "mermaid": ""},
                                            "method": "Inspected the diff.",
                                            "assessment": "ready",
                                            "strengths": [],
                                            "description_notes": [],
                                            "findings": [],
                                        }
                                    ),
                                },
                            }
                        ),
                    ]
                ),
                stderr="",
            )
            head = CompletedProcess(args=[], returncode=0, stdout=f"{expected_head}\n", stderr="")
            with (
                patch.object(
                    multi_model_review,
                    "run",
                    side_effect=[head, timed_out, reviewed],
                ) as run,
                patch.dict(multi_model_review.LANE_MODELS, {"antigravity": ""}),
            ):
                result = multi_model_review.lane_antigravity("prompt", td, worktree)

            self.assertEqual(result.code, 0)
            self.assertTrue((worktree / "antigravity.attempt1.stream.jsonl").exists())
            self.assertEqual(len(run.call_args_list), 3)

    def test_antigravity_surfaces_stream_error_after_retry(self) -> None:
        timed_out = CompletedProcess(
            args=[],
            returncode=1,
            stdout=json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "ERROR",
                        "response": "",
                        "error": "timeout waiting for response",
                    },
                }
            ),
            stderr="",
        )

        with (
            tempfile.TemporaryDirectory() as td,
            patch.object(
                multi_model_review,
                "run",
                side_effect=[
                    CompletedProcess(args=[], returncode=0, stdout=f"{'a' * 40}\n", stderr=""),
                    timed_out,
                    timed_out,
                ],
            ),
            patch.dict(multi_model_review.LANE_MODELS, {"antigravity": ""}),
        ):
            result = multi_model_review.lane_antigravity("prompt", td, Path(td))

        self.assertEqual(result.code, 1)
        self.assertIn("timeout waiting for response", result.err)

    def test_antigravity_rejects_missing_checkout_provenance(self) -> None:
        stream = json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "review"}})
        response, error = multi_model_review._parse_antigravity_stream(
            stream,
            worktree=Path("/tmp/review-worktree"),
            provenance_cmd="git -C /tmp/review-worktree rev-parse HEAD",
            expected_head="a" * 40,
        )

        self.assertEqual(response, "")
        self.assertIn("did not prove the review checkout", error or "")

    def test_antigravity_rejects_repository_tool_cwd_outside_worktree(self) -> None:
        expected_head = "a" * 40
        provenance_cmd = "git -C /tmp/review-worktree rev-parse HEAD"
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {"CommandLine": provenance_cmd},
                                "output": expected_head,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {
                                    "CommandLine": "git status",
                                    "Cwd": "/tmp/antigravity-cli/scratch/repository",
                                },
                            },
                        },
                    }
                ),
                json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "review"}}),
            ]
        )

        response, error = multi_model_review._parse_antigravity_stream(
            stream,
            worktree=Path("/tmp/review-worktree"),
            provenance_cmd=provenance_cmd,
            expected_head=expected_head,
        )

        self.assertEqual(response, "")
        self.assertIn("escaped the review worktree", error or "")

    def test_antigravity_allows_scratch_file_in_system_temp_directory(self) -> None:
        expected_head = "a" * 40
        provenance_cmd = "git -C /tmp/review-worktree rev-parse HEAD"
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {"CommandLine": provenance_cmd},
                                "output": expected_head,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "view_file",
                                "parameters": {"AbsolutePath": "/tmp/old_rfq_read_service.py"},
                            },
                        },
                    }
                ),
                json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "review"}}),
            ]
        )

        response, error = multi_model_review._parse_antigravity_stream(
            stream,
            worktree=Path("/tmp/review-worktree"),
            provenance_cmd=provenance_cmd,
            expected_head=expected_head,
        )

        self.assertEqual(response, "review")
        self.assertIsNone(error)

    def test_antigravity_allows_file_in_cli_scratch_directory(self) -> None:
        expected_head = "a" * 40
        provenance_cmd = "git -C /tmp/review-worktree rev-parse HEAD"
        scratch_file = Path.home() / ".gemini" / "antigravity-cli" / "scratch" / "diff.patch"
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {"CommandLine": provenance_cmd},
                                "output": expected_head,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "view_file",
                                "parameters": {"AbsolutePath": str(scratch_file)},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "review"},
                    }
                ),
            ]
        )

        response, error = multi_model_review._parse_antigravity_stream(
            stream,
            worktree=Path("/tmp/review-worktree"),
            provenance_cmd=provenance_cmd,
            expected_head=expected_head,
        )

        self.assertEqual(response, "review")
        self.assertIsNone(error)

    def test_antigravity_rejects_cli_scratch_as_repository_cwd(self) -> None:
        expected_head = "a" * 40
        provenance_cmd = "git -C /tmp/review-worktree rev-parse HEAD"
        scratch_directory = Path.home() / ".gemini" / "antigravity-cli" / "scratch"
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {"CommandLine": provenance_cmd},
                                "output": expected_head,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {
                                    "CommandLine": "git status",
                                    "Cwd": str(scratch_directory),
                                },
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "result",
                        "result": {"status": "SUCCESS", "response": "review"},
                    }
                ),
            ]
        )

        response, error = multi_model_review._parse_antigravity_stream(
            stream,
            worktree=Path("/tmp/review-worktree"),
            provenance_cmd=provenance_cmd,
            expected_head=expected_head,
        )

        self.assertEqual(response, "")
        self.assertIn("escaped the review worktree", error or "")

    def test_antigravity_rejects_file_outside_worktree_and_system_temp_directory(self) -> None:
        expected_head = "a" * 40
        provenance_cmd = "git -C /tmp/review-worktree rev-parse HEAD"
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {"CommandLine": provenance_cmd},
                                "output": expected_head,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "view_file",
                                "parameters": {"AbsolutePath": "/opt/unrelated-checkout/service.py"},
                            },
                        },
                    }
                ),
                json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "review"}}),
            ]
        )

        response, error = multi_model_review._parse_antigravity_stream(
            stream,
            worktree=Path("/tmp/review-worktree"),
            provenance_cmd=provenance_cmd,
            expected_head=expected_head,
        )

        self.assertEqual(response, "")
        self.assertIn("escaped the review worktree", error or "")

    def test_antigravity_allows_sed_range_expressions_in_worktree(self) -> None:
        expected_head = "a" * 40
        provenance_cmd = "git -C /tmp/review-worktree rev-parse HEAD"
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {"CommandLine": provenance_cmd},
                                "output": expected_head,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "step_update",
                        "step_update": {
                            "state": "DONE",
                            "tool_info": {
                                "name": "run_command",
                                "parameters": {
                                    "CommandLine": "sed -n '/async def evaluate/,/def /p' api/trading_engine/engine.py",
                                    "Cwd": "/tmp/review-worktree",
                                },
                            },
                        },
                    }
                ),
                json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "review"}}),
            ]
        )

        response, error = multi_model_review._parse_antigravity_stream(
            stream,
            worktree=Path("/tmp/review-worktree"),
            provenance_cmd=provenance_cmd,
            expected_head=expected_head,
        )

        self.assertEqual(response, "review")
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
