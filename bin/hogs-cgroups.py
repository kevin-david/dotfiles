#!/usr/bin/env python3
"""Read-only cgroup v2 attribution for hogs. Requires only Python's stdlib."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time


DOCKER_SCOPE = re.compile(r"docker-([0-9a-f]{64})\.scope$")


def read(path):
    try:
        return path.read_text().strip()
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return ""


def counters(path):
    return {parts[0]: int(parts[1]) for line in read(path).splitlines()
            if len(parts := line.split()) == 2 and parts[1].isdigit()}


def pressure(path):
    for line in read(path / "memory.pressure").splitlines():
        if line.startswith("full "):
            return float(dict(part.split("=") for part in line.split()[1:])["avg10"])
    return None


def groups(root):
    # Environment rows partition the tree; the shared pool is diagnostic only.
    envs = []
    for path in sorted(root.iterdir()):
        if not path.is_dir():
            continue
        if path.name == "lxc":
            yield path, "pool", "lxc pool", None
            envs.extend((p, "lxc" + p.name) for p in sorted(path.iterdir())
                        if p.is_dir() and p.name.isdigit())
        elif path.name == "qemu.slice" and list(path.glob("*.scope")):
            envs.extend((p, "vm:" + p.stem) for p in sorted(path.glob("*.scope")))
        else:
            envs.append((path, "host:" + path.name))
    for path, env in envs:
        yield path, "env", env, None
        for child in sorted(path.rglob("docker-*.scope")):
            match = DOCKER_SCOPE.fullmatch(child.name)
            if match:
                yield child, "service", env, match[1]


def snapshot(root, clock=time.monotonic):
    result = {}
    for path, kind, env, container_id in groups(root):
        try:
            inode = path.stat().st_ino
        except FileNotFoundError:
            continue
        started = clock()
        cpu = counters(path / "cpu.stat")
        events = counters(path / "memory.events.local")
        stamp = (started + clock()) / 2
        if not cpu:
            continue
        current = read(path / "memory.current")
        result[str(path.relative_to(root))] = {
            "path": path, "kind": kind, "env": env, "id": container_id,
            "inode": inode, "time": stamp, "cpu": cpu, "events": events,
            "memory": int(current) if current.isdigit() else None,
            "high": read(path / "memory.high"), "max": read(path / "memory.max"),
            "pressure": pressure(path), "cpus": read(path / "cpuset.cpus.effective"),
            "weight": read(path / "cpu.weight"),
        }
    return result


def delta(before, after, field, key):
    if before is None or before["inode"] != after["inode"]:
        return None
    old, new = before[field].get(key), after[field].get(key)
    if old is None or new is None or new < old:
        return None
    return new - old


def cores(before, after):
    usage = delta(before, after, "cpu", "usage_usec")
    if usage is None or after["time"] <= before["time"]:
        return None
    return usage / 1_000_000 / (after["time"] - before["time"])


def quota(path, root):
    limits, available = [], False
    for ancestor in (path, *path.parents):
        if ancestor != root and root not in ancestor.parents:
            break
        parts = read(ancestor / "cpu.max").split()
        if len(parts) == 2:
            available = True
            if parts[0] != "max" and int(parts[1]) > 0:
                limits.append((int(parts[0]) / int(parts[1]), ancestor))
    if not limits:
        return "unlimited" if available else "?"
    value, ancestor = min(limits, key=lambda pair: pair[0])
    suffix = "" if ancestor == path else f"@{ancestor.relative_to(root)}(shared)"
    return f"{value:.2f}{suffix}"


def clean(value):
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(value))


def effective_cpus(path, root):
    for ancestor in (path, *path.parents):
        if ancestor != root and root not in ancestor.parents:
            break
        value = read(ancestor / "cpuset.cpus.effective")
        if value:
            return value
    return "?"


def name(record, names):
    if not record["id"]:
        return record["env"]
    env = record["env"] if record["env"].startswith("lxc") else "host"
    key = f"{env}:{record['id'][:12]}"
    return names.get(key, f"{env}:docker:{record['id'][:12]}")


def gib(value):
    if value is None:
        return "?"
    return f"{int(value) / 2**30:.2f}" if str(value).isdigit() else (value or "?")


def report(root, before, after, names, count):
    rates = {key: cores(before.get(key), row) for key, row in after.items()}
    order = lambda key: rates[key] if rates[key] is not None else -1
    envs = sorted((k for k, r in after.items() if r["kind"] == "env"), key=order, reverse=True)
    services = sorted((k for k, r in after.items() if r["kind"] == "service"), key=order, reverse=True)
    durations = [after[k]["time"] - before[k]["time"] for k in after
                 if k in before and rates[k] is not None]
    window = f"{min(durations):.2f}–{max(durations):.2f}s" if durations else "unavailable"
    print(f"\n=================== CPU BY ENV / SERVICE ({window}, measured per cgroup) ===================")
    print("Parent totals include children. Do not add parent and service rows together.")
    print(f"Showing the top {count} Docker services across environments; residuals use every sampled service.")
    print(f"{'OWNER':<48} {'CORES':>7} {'MEM_GiB':>8}  QUOTA_CORES / THROTTLING / EFFECTIVE_CPUSET")
    selected = set(services[:count])
    for key in envs[:count]:
        row = after[key]
        children = [k for k in services if after[k]["env"] == row["env"]]
        for item in [key] + [k for k in children if k in selected]:
            record = after[item]
            rate = "?" if rates[item] is None else f"{rates[item]:.2f}"
            throttle = delta(before.get(item), record, "cpu", "nr_throttled")
            throttle_us = delta(before.get(item), record, "cpu", "throttled_usec")
            throttle_ms = "?" if throttle_us is None else f"{throttle_us / 1000:.0f}"
            label = ("  " if item != key else "") + name(record, names)
            print(f"{label:<48} {rate:>7} {gib(record['memory']):>8}  "
                  f"quota={quota(record['path'], root)} weight={record['weight'] or '?'} "
                  f"throttled={throttle if throttle is not None else '?'} events/{throttle_ms}ms "
                  f"cpus={effective_cpus(record['path'], root)}")
        omitted = [k for k in children if k not in selected]
        if omitted:
            total = sum(rates[k] for k in omitted if rates[k] is not None)
            unknown = sum(rates[k] is None for k in omitted)
            print(f"  other Docker services ({len(omitted)}): {total:.2f} cores" +
                  (f"; {unknown} unsampled" if unknown else ""))
        if rates[key] is not None and children and all(rates[k] is not None for k in children):
            residual = rates[key] - sum(rates[k] for k in children)
            print(f"  non-Docker/sample residual: {residual:.2f} cores; "
                  "includes shells, tests and agents outside Docker (small negatives are sample skew)")

    print("\n=================== CGROUP MEMORY (GiB; local event deltas) ===================")
    print(f"{'OWNER':<48} {'USED':>7} {'HIGH':>7} {'MAX':>7} {'FULL%10s':>9}  NEW high/max/oom/kill")
    ranked = sorted(after, key=lambda k: after[k]["memory"] or 0, reverse=True)
    # Always retain the shared parent and any group with new limit events.
    selected_mem = [k for k in ranked if after[k]["kind"] == "pool"]
    selected_mem += [k for k in ranked if k not in selected_mem and any(
        (delta(before.get(k), after[k], "events", event) or 0) > 0
        for event in ("high", "max", "oom", "oom_kill"))]
    selected_mem += [k for k in ranked if k not in selected_mem][:count]
    for key in selected_mem:
        row = after[key]
        changes = [delta(before.get(key), row, "events", event)
                   for event in ("high", "max", "oom", "oom_kill")]
        events = "/".join("?" if value is None else str(value) for value in changes)
        psi = "?" if row["pressure"] is None else f"{row['pressure']:.2f}"
        print(f"{name(row, names):<48} {gib(row['memory']):>7} {gib(row['high']):>7} "
              f"{gib(row['max']):>7} {psi:>9}  {events}")
        if changes[0] and row["pressure"] is not None and row["pressure"] > 5:
            print(f"  CGROUP RECLAIM: {name(row, names)}: high +{changes[0]}, "
                  f"full PSI {psi}%; limit pressure can occur despite available host RAM")
    print("Memory parent rows include descendants; ? means unavailable/reset/new, max means no local limit.")
    print("Children also share ancestor limits. Check the pool row before blaming host RAM or ARC.")
    if not envs or rates[envs[0]] is None:
        return "aggregate CPU sample unavailable"
    top = envs[0]
    result = f"{name(after[top], names)}: {rates[top]:.2f} cores"
    leaders = [k for k in services if after[k]["env"] == after[top]["env"] and rates[k] is not None][:3]
    if leaders:
        result += "; services: " + ", ".join(f"{name(after[k], names)} {rates[k]:.2f} cores" for k in leaders)
    return result


def docker_snapshot(env, timeout=8):
    command = ["docker"] if env == "host" else [
        "pct", "exec", env[3:], "--", "/run/current-system/sw/bin/docker"]
    deadline = time.monotonic() + timeout

    def run(args):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, timeout)
        with subprocess.Popen(command + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True) as process:
            try:
                output, _ = process.communicate(timeout=remaining)
            except subprocess.TimeoutExpired:
                # A timed-out pct wrapper can leave a Docker client holding its pipes.
                # This group contains only the read-only lookup we just started.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command + args)
            return output
    try:
        ids = run(["ps", "-aq"]).split()
        if not ids:
            return {}, ""
        # Print only selected metadata; container environments may contain secrets.
        lines = run(["inspect", "--format",
                     '{{json .Id}}|{{json .Name}}|{{.RestartCount}}|{{json .State.Status}}|{{json .State.StartedAt}}', *ids])
        result = {}
        for line in lines.splitlines():
            cid, label, restarts, status, started = line.split("|", 4)
            result[json.loads(cid)] = {"Name": clean(json.loads(label).lstrip("/")),
                "RestartCount": int(restarts), "State": {"Status": json.loads(status), "StartedAt": json.loads(started)}}
        return result, ""
    except subprocess.TimeoutExpired:
        return {}, f"{env}: Docker metadata timed out after {timeout}s; using cgroup IDs"
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        # Do not echo subprocess output, which is not needed for diagnosis.
        return {}, f"{env}: Docker metadata unavailable ({type(error).__name__}); using cgroup IDs"


def restart_note(before, after):
    if after["State"]["Status"] == "restarting":
        return "currently restarting"
    if before is None or after["RestartCount"] < before["RestartCount"]:
        return "history; delta unavailable"
    change = after["RestartCount"] - before["RestartCount"]
    if change:
        return f"restarts observed +{change}; now {after['State']['Status']}"
    return "history; no restart observed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/sys/fs/cgroup"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--count", type=int, default=20)
    args = parser.parse_args()
    if not (args.root / "cgroup.controllers").exists():
        print("cgroup v2 unavailable; aggregate attribution skipped")
        return
    print("Sampling cgroups; Docker name/restart lookups have an 8s budget per environment per pass.", flush=True)
    environments = sorted({env for _, kind, env, _ in groups(args.root)
                           if kind == "env" and env.startswith("lxc")})
    if shutil.which("docker"):
        environments.insert(0, "host")
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = {env: pool.submit(docker_snapshot, env) for env in environments}
        first = {env: future.result() for env, future in first.items()}
        # Keep Docker/pct metadata collection out of the CPU measurement window.
        before = snapshot(args.root)
        time.sleep(2)
        after = snapshot(args.root)
        second = {env: pool.submit(docker_snapshot, env) for env, (_, error) in first.items() if not error}
        second = {env: future.result() for env, future in second.items()}
    names, restarts = {}, []
    for env, (old, error) in first.items():
        new, second_error = second.get(env, ({}, ""))
        for warning in (error, second_error):
            if warning:
                print("  " + warning)
        for cid, record in {**old, **new}.items():
            names[f"{env}:{cid[:12]}"] = f"{env}:{record['Name']}"
            note = restart_note(old.get(cid), record) if cid in new else "history; second sample unavailable"
            if record["RestartCount"] >= 5 or record["State"]["Status"] == "restarting" or note.startswith("restarts observed"):
                restarts.append((env, record, note))
    top = report(args.root, before, after, names, args.count)
    (args.output_dir / "names").write_text("".join(f"{key}\t{value}\n" for key, value in names.items()))
    (args.output_dir / "aggregate").write_text(top + "\n")
    print("\n=================== RESTART OBSERVATIONS (two metadata reads, separate from CPU window) ===================")
    if not restarts:
        print("No restart evidence in successful metadata samples; failed lookups remain unknown.")
    restarts.sort(key=lambda item: (not (item[2].startswith("restarts observed") or item[2] == "currently restarting"), -item[1]["RestartCount"]))
    for env, record, note in restarts[:args.count]:
        print(f"{env}:{record['Name']:<40} total={record['RestartCount']} "
              f"state={record['State']['Status']} started={record['State']['StartedAt']}  {note}")
    print("Recent startup plus historical failures alone does not establish an active restart loop.")


if __name__ == "__main__":
    main()
