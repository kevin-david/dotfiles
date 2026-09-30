#!/usr/bin/env python3
"""Regression cases for attribution during contention, without Docker or root."""
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "hogs_cgroups", Path(__file__).resolve().parents[1] / "bin/hogs-cgroups.py"
)
hogs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hogs)


class HogsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def group(self, name, usage=0, high="max", events=0, pressure=0):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        files = {
            "cpu.stat": f"usage_usec {usage}\nnr_throttled 0\nthrottled_usec 0\n",
            "cpu.max": "max 100000", "cpu.weight": "50",
            "cpuset.cpus.effective": "0-7", "memory.current": str(6 * 2**30),
            "memory.high": high, "memory.max": str(10 * 2**30),
            "memory.events.local": f"high {events}\nmax 0\noom 0\noom_kill 0\n",
            "memory.pressure": f"some avg10={pressure} avg60=0 total=0\nfull avg10={pressure} avg60=0 total=0\n",
        }
        for filename, value in files.items():
            (path / filename).write_text(value)
        return path

    def test_aggregate_workers_without_adding_parent_to_children(self):
        service = "lxc/201/ns/system.slice/docker-" + "a" * 64 + ".scope"
        self.group("lxc")
        self.group("lxc/201")
        self.group(service)
        before = hogs.snapshot(self.root, clock=lambda: 10)
        self.group("lxc/201", usage=20_000_000)
        self.group(service, usage=16_000_000)
        after = hogs.snapshot(self.root, clock=lambda: 14)
        output = io.StringIO()
        with patch("sys.stdout", output):
            top = hogs.report(self.root, before, after, {}, 10)
        self.assertIn("lxc201: 5.00 cores", top)
        self.assertIn("4.00 cores", top)
        self.assertIn("non-Docker/sample residual: 1.00 cores", output.getvalue())
        self.assertNotIn("9.00 cores", top)

    def test_shared_parent_reclaim_is_reported_without_child_events(self):
        self.group("lxc", high=str(6 * 2**30), events=40, pressure=60)
        self.group("lxc/201")
        before = hogs.snapshot(self.root, clock=lambda: 0)
        self.group("lxc", high=str(6 * 2**30), events=65, pressure=60)
        after = hogs.snapshot(self.root, clock=lambda: 4)
        output = io.StringIO()
        with patch("sys.stdout", output):
            hogs.report(self.root, before, after, {}, 1)
        self.assertIn("CGROUP RECLAIM: lxc pool", output.getvalue())
        self.assertIn("high +25", output.getvalue())

    def test_recreated_cgroup_has_no_fake_cpu_delta(self):
        self.group("lxc/201", usage=8_000_000)
        before = hogs.snapshot(self.root, clock=lambda: 2)
        after = hogs.snapshot(self.root, clock=lambda: 6)
        after["lxc/201"]["inode"] += 1
        self.assertIsNone(hogs.delta(before["lxc/201"], after["lxc/201"], "cpu", "usage_usec"))

    def test_cpu_quota_reports_shared_ancestor(self):
        parent = self.group("lxc/201")
        service = self.group("lxc/201/ns/system.slice/docker-" + "a" * 64 + ".scope")
        (parent / "cpu.max").write_text("200000 100000")
        self.assertEqual(hogs.quota(service, self.root), "2.00@lxc/201(shared)")
        (service / "cpuset.cpus.effective").unlink()
        self.assertEqual(hogs.effective_cpus(service, self.root), "0-7")

    def test_recent_start_does_not_prove_restart_loop(self):
        old = {"RestartCount": 10, "State": {"Status": "running"}}
        new = {"RestartCount": 10, "State": {"Status": "running"}}
        self.assertEqual(hogs.restart_note(old, new), "history; no restart observed")
        new["RestartCount"] = 11
        self.assertEqual(hogs.restart_note(old, new), "restarts observed +1; now running")
        new["State"]["Status"] = "exited"
        self.assertIn("now exited", hogs.restart_note(old, new))
        self.assertEqual(hogs.restart_note(None, new), "history; delta unavailable")

    def test_metadata_timeout_is_explicit(self):
        executable = self.root / "docker"
        executable.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n")
        executable.chmod(0o755)
        started = time.monotonic()
        with patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]}):
            records, error = hogs.docker_snapshot("host", timeout=0.1)
        self.assertEqual(records, {})
        self.assertIn("timed out", error)
        self.assertLess(time.monotonic() - started, 2)

    def test_missing_counter_is_unknown_instead_of_zero(self):
        path = self.group("lxc/201")
        (path / "memory.events.local").unlink()
        before = hogs.snapshot(self.root, clock=lambda: 1)
        after = hogs.snapshot(self.root, clock=lambda: 3)
        self.assertIsNone(hogs.delta(before["lxc/201"], after["lxc/201"], "events", "high"))


if __name__ == "__main__":
    unittest.main()
