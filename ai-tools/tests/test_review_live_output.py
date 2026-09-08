from __future__ import annotations

import importlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))
review = importlib.import_module("multi_model_review")


class LiveOutputTest(unittest.TestCase):
    def test_partial_output_survives_runner_cancellation(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            stdout = b'{"type":"thread.started","thread_id":"active-session"}\npartial stdout'
            child = f"import os, time; os.write(1, {stdout!r}); os.write(2, b'partial stderr'); time.sleep(30)"
            runner = (
                f"import sys; sys.path.insert(0, {str(TOOLS_DIR)!r}); "
                "from pathlib import Path; from multi_model_review import run; "
                f"run([sys.executable, '-c', {child!r}], "
                f"output_files=(Path({str(out / 'codex.stdout')!r}), Path({str(out / 'codex.err')!r})))"
            )
            process = subprocess.Popen([sys.executable, "-c", runner], start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if (out / "codex.err").exists() and (out / "codex.err").read_bytes() == b"partial stderr":
                        break
                    time.sleep(0.01)
                self.assertIsNone(process.poll())
                self.assertEqual((out / "codex.stdout").read_bytes(), stdout)
                self.assertEqual((out / "codex.err").read_bytes(), b"partial stderr")
                self.assertIn("output bytes; last output", review.lane_output_activity("codex", out))
                self.assertEqual(review.session_id_from_output("codex", stdout.decode()), "active-session")
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            self.assertEqual((out / "codex.stdout").read_bytes(), stdout)
            self.assertEqual((out / "codex.err").read_bytes(), b"partial stderr")

    def test_completion_keeps_output_and_exit_status(self):
        for code in (0, 7):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as td:
                out = Path(td)
                command = [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.stdin.read() + ' café'); print('diagnostic', file=sys.stderr); "
                    f"sys.exit({code})",
                ]
                result = review.run(command, input="request", output_files=(out / "stdout", out / "stderr"))
                self.assertEqual(result.returncode, code)
                self.assertEqual(result.stdout, "request café\n")
                self.assertEqual(result.stderr, "diagnostic\n")
                self.assertEqual((out / "stdout").read_text(), result.stdout)
                self.assertEqual((out / "stderr").read_text(), result.stderr)

    def test_empty_files_do_not_count_as_output(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            self.assertEqual(review.lane_output_activity("codex", out), "no output observed yet")
            (out / "codex.stdout").touch()
            (out / "codex.err").touch()
            self.assertEqual(review.lane_output_activity("codex", out), "no output observed yet")

    def test_claude_stream_keeps_only_terminal_review_and_session_identity(self):
        final = '<<<REVIEW_JSON\n{"assessment":"ready","findings":[]}\nREVIEW_JSON>>>'
        stream = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "system", "session_id": "review-session"},
                {"type": "stream_event", "event": {"type": "content_block_delta", "delta": "working"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "progress"}]}},
                {"type": "result", "session_id": "review-session", "result": final, "is_error": False},
            )
        )
        response = CompletedProcess([], 0, stream, "")
        with tempfile.TemporaryDirectory() as td, patch.object(review, "run", return_value=response) as run:
            out = Path(td)
            result = review.lane_claude("review", td, out)
            self.assertEqual(result.out, final)
            self.assertEqual(result.code, 0)
            self.assertEqual(review.extract_findings(result.out)["assessment"], "ready")
            self.assertEqual(review.session_id_from_output("claude", stream), "review-session")
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
            self.assertIn("--include-partial-messages", command)
            self.assertIn("--verbose", command)
            self.assertEqual(run.call_args.kwargs["output_files"], (out / "claude.stdout", out / "claude.err"))

    def test_claude_partial_stream_is_not_a_completed_review(self):
        response = CompletedProcess([], 0, '{"type":"system","session_id":"session"}\n', "")
        with tempfile.TemporaryDirectory() as td, patch.object(review, "run", return_value=response):
            result = review.lane_claude("review", td, Path(td))
            self.assertEqual(result.out, "")
            self.assertEqual(result.code, 1)
            self.assertIn("without a result event", result.err)

    def test_streamed_tool_error_does_not_trigger_model_retry(self):
        stream = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "user", "message": {"content": "This fixture tests model unavailable errors"}},
                {"type": "result", "result": "review complete", "is_error": False},
            )
        )
        response = CompletedProcess([], 0, stream, "")
        with tempfile.TemporaryDirectory() as td, patch.object(review, "run", return_value=response) as run:
            result = review.lane_claude("review", td, Path(td))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result.out, "review complete")

    def test_claude_terminal_error_without_result_keeps_diagnostic(self):
        response = CompletedProcess(
            [],
            0,
            '{"type":"result","subtype":"error_during_execution","is_error":true,"errors":["request failed"]}',
            "",
        )
        with tempfile.TemporaryDirectory() as td, patch.object(review, "run", return_value=response):
            result = review.lane_claude("review", td, Path(td))
            self.assertEqual(result.code, 1)
            self.assertEqual(result.out, "")
            self.assertIn("request failed", result.err)

    def test_claude_terminal_error_is_not_success(self):
        response = CompletedProcess([], 0, '{"type":"result","is_error":true,"result":"request failed"}\n', "")
        with tempfile.TemporaryDirectory() as td, patch.object(review, "run", return_value=response):
            result = review.lane_claude("review", td, Path(td))
            self.assertEqual(result.code, 1)


if __name__ == "__main__":
    unittest.main()
