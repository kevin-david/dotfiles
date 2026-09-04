#!/usr/bin/env python3
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[1]
WRAPPER = REPO / "bin" / "codex"


HELPER_SOURCE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc == 4 && strcmp(argv[1], "app-server") == 0 &&
        strcmp(argv[2], "daemon") == 0 &&
        strcmp(argv[3], "pid-update-loop") == 0) {
        pid_t child = fork();
        if (child < 0) return 2;
        if (child == 0) _exit(0);
        printf("%d\n", child);
        fflush(stdout);
        sleep(300);
        return 0;
    }

    const char *marker = getenv("CODEX_TEST_MARKER");
    if (marker == NULL) return 3;
    FILE *file = fopen(marker, "w");
    if (file == NULL) return 4;
    for (int i = 1; i < argc; i++) fprintf(file, "%s\n", argv[i]);
    fclose(file);
    return 0;
}
"""


def process_start_time(pid: int) -> str:
    return subprocess.check_output(
        ["ps", "-p", str(pid), "-o", "lstart="], text=True
    ).strip()


def wait_for_state(pid: int, state: str) -> None:
    for _ in range(100):
        status = Path(f"/proc/{pid}/status")
        if status.exists():
            for line in status.read_text().splitlines():
                if line.startswith("State:") and line.split()[1] == state:
                    return
        time.sleep(0.01)
    raise AssertionError(f"PID {pid} did not reach state {state}")


class CodexWrapperTest(unittest.TestCase):
    def test_recovers_zombie_managed_daemon_before_remote_control(self) -> None:
        cc = shutil.which("cc")
        self.assertIsNotNone(cc, "the process fixture needs a C compiler")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            home = temp / "home"
            codex_home = home / ".codex-test-host"
            daemon_dir = codex_home / "app-server-daemon"
            control_dir = codex_home / "app-server-control"
            bin_dir = home / ".codex" / "packages" / "standalone" / "current" / "bin"
            daemon_dir.mkdir(parents=True)
            control_dir.mkdir(parents=True)
            bin_dir.mkdir(parents=True)

            source = temp / "codex-helper.c"
            helper = bin_dir / "codex"
            source.write_text(HELPER_SOURCE)
            subprocess.run([cc, str(source), "-o", str(helper)], check=True)

            manager = subprocess.Popen(
                [str(helper), "app-server", "daemon", "pid-update-loop"],
                stdout=subprocess.PIPE,
                text=True,
            )
            def cleanup_manager() -> None:
                if manager.poll() is None:
                    manager.kill()
                manager.wait()
                if manager.stdout is not None:
                    manager.stdout.close()

            self.addCleanup(cleanup_manager)
            assert manager.stdout is not None
            zombie_pid = int(manager.stdout.readline().strip())
            wait_for_state(zombie_pid, "Z")

            (daemon_dir / "app-server.pid").write_text(
                json.dumps(
                    {
                        "pid": zombie_pid,
                        "processStartTime": process_start_time(zombie_pid),
                    }
                )
            )
            (daemon_dir / "app-server-updater.pid").write_text(
                json.dumps(
                    {
                        "pid": manager.pid,
                        "processStartTime": process_start_time(manager.pid),
                    }
                )
            )

            marker = temp / "invocation"
            env = os.environ | {
                "HOME": str(home),
                "CODEX_HOME": str(codex_home),
                "CODEX_TEST_MARKER": str(marker),
            }
            result = subprocess.run(
                [str(WRAPPER), "remote-control", "start"],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(manager.poll(), "the stale manager is still running")
            self.assertFalse(Path(f"/proc/{zombie_pid}").exists())
            self.assertEqual(marker.read_text(), "remote-control\nstart\n")
            self.assertIn("recovering unreaped app-server", result.stderr)

            marker.unlink()
            other_manager = subprocess.Popen(
                [str(helper), "app-server", "daemon", "pid-update-loop"],
                stdout=subprocess.PIPE,
                text=True,
            )

            def cleanup_other_manager() -> None:
                if other_manager.poll() is None:
                    other_manager.kill()
                other_manager.wait()
                if other_manager.stdout is not None:
                    other_manager.stdout.close()

            self.addCleanup(cleanup_other_manager)
            assert other_manager.stdout is not None
            other_zombie_pid = int(other_manager.stdout.readline().strip())
            wait_for_state(other_zombie_pid, "Z")
            (daemon_dir / "app-server.pid").write_text(
                json.dumps(
                    {
                        "pid": other_zombie_pid,
                        "processStartTime": process_start_time(other_zombie_pid),
                    }
                )
            )
            (daemon_dir / "app-server-updater.pid").write_text(
                json.dumps(
                    {
                        "pid": other_manager.pid,
                        "processStartTime": "mismatched start time",
                    }
                )
            )

            refused = subprocess.run(
                [str(WRAPPER), "remote-control", "start"],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(refused.returncode, 0, refused.stderr)
            self.assertIsNone(other_manager.poll(), "an unverified manager was stopped")
            self.assertNotIn("recovering unreaped app-server", refused.stderr)
            self.assertEqual(marker.read_text(), "remote-control\nstart\n")


if __name__ == "__main__":
    unittest.main()
