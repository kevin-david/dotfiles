from __future__ import annotations

import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

review = importlib.import_module("multi_model_review")


def codex_log(*events: dict[str, object]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def command_event(item_id: str, command: str) -> dict[str, object]:
    return {"type": "item.started", "item": {"id": item_id, "type": "command_execution", "command": command}}


class LaneProgressTest(unittest.TestCase):
    def test_progress_file_states_each_lane_without_reprinting_its_event_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "codex.stdout").write_text(
                codex_log(
                    {"type": "thread.started", "thread_id": "codex-session"},
                    {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "reading"}},
                    command_event("i1", "rg -n quote_policy"),
                )
            )
            (out / "antigravity.stream.jsonl").write_text(
                codex_log(
                    {"event": "init", "conversation_id": "antigravity-session"},
                    {"event": "step_update", "step_update": {"step_index": 4, "state": "RUNNING", "step_type": "tool"}},
                )
            )
            (out / "claude.stdout").write_text("")

            launched = time.monotonic()
            review.write_progress_file(
                out,
                ["claude", "codex", "antigravity"],
                start=dict.fromkeys(("claude", "codex", "antigravity"), launched),
                done={"antigravity": launched + 12},
            )
            progress = (out / "progress.txt").read_text()

            self.assertIn("codex: running", progress)
            self.assertIn("started command_execution: rg -n quote_policy", progress)
            self.assertIn("session codex-session", progress)
            self.assertIn("antigravity: finished after 12s", progress)
            self.assertIn("step 4 RUNNING tool", progress)
            self.assertIn("session antigravity-session", progress)
            # A lane whose CLI has emitted nothing yet says so, rather than
            # reading as either healthy or wedged.
            self.assertIn("claude: running after 0s — no output observed yet", progress)
            self.assertNotIn("reading", progress)

    def test_progress_summary_is_bounded_while_the_event_log_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            filler = codex_log(
                *(
                    {"type": "item.completed", "item": {"id": f"i{n}", "type": "agent_message", "text": "x" * 400}}
                    for n in range(4000)
                )
            )
            (out / "codex.stdout").write_text(
                codex_log({"type": "thread.started", "thread_id": "codex-session"})
                + codex_log(command_event("old", "OLDEST"))
                + filler
                + codex_log(command_event("last", "NEWEST"))
            )
            self.assertGreater((out / "codex.stdout").stat().st_size, 1_000_000)

            review.write_progress_file(out, ["codex"], start={"codex": time.monotonic()}, done={})
            progress = (out / "progress.txt").read_text()

            self.assertIn("NEWEST", progress)
            # Only the tail is scanned, so an event from earlier in a multi-megabyte
            # log cannot reach the summary — that is what keeps it small.
            self.assertNotIn("OLDEST", progress)
            self.assertLess(len(progress), 2048)

    def test_long_event_detail_is_clipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "codex.stdout").write_text(codex_log(command_event("i0", "y" * 900)))
            summary = review.lane_progress("codex", out)
            self.assertIn("y" * 100, summary)
            self.assertLess(len(summary), 400)
            self.assertTrue(summary.endswith("…"))

    def test_stderr_tail_and_recorded_session_reach_the_summary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "codex.stdout").write_text(codex_log({"type": "turn.completed", "usage": {"output_tokens": 12}}))
            (out / "codex.err").write_text("first diagnostic\nstream disconnected\n")
            (out / "codex.session.json").write_text(json.dumps({"session_id": "recorded-session"}))

            summary = review.lane_progress("codex", out)
            self.assertIn("stderr: stream disconnected", summary)
            self.assertNotIn("first diagnostic", summary)
            self.assertIn("session recorded-session", summary)
            self.assertIn("turn.completed", summary)

    def test_unparsed_output_is_not_described_as_an_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "codex.stdout").write_text("not json at all\n")
            summary = review.lane_progress("codex", out)
            self.assertIn("output bytes; last output", summary)
            self.assertNotIn("not json", summary)

    def test_partial_trailing_line_falls_back_to_the_last_complete_event(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "codex.stdout").write_text(codex_log(command_event("i0", "rg -n devig")) + '{"type": "item.st')
            self.assertIn("started command_execution: rg -n devig", review.lane_progress("codex", out))

    def test_claude_terminal_result_event_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "claude.stdout").write_text(
                codex_log({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Grep"}]}})
            )
            self.assertIn("tool Grep", review.lane_progress("claude", out))


if __name__ == "__main__":
    unittest.main()
