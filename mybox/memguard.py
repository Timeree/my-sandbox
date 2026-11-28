import os
import signal
import sys
import time

INTERVAL = 6


def cgroup_usage():
    """Return (used, total) from cgroup v2, minus reclaimable file cache."""
    base = "/sys/fs/cgroup"
    try:
        used = int(open(f"{base}/memory.current").read())
        raw = open(f"{base}/memory.max").read().strip()
        total = int(raw) if raw.isdigit() else 0
    except (OSError, ValueError):
        return 0, 0
    if not total:
        return 0, 0
    try:
        with open(f"{base}/memory.stat") as f:
            for line in f:
                if line.startswith("inactive_file "):
                    used -= int(line.split()[1])
                    break
    except (OSError, ValueError):
        pass
    return max(used, 0), total


def meminfo_usage():
    info = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError):
        return 0, 0
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    return max(total - avail, 0), total


def usage():
    used, total = cgroup_usage()
    return (used, total) if total else meminfo_usage()


def top_process(skip):
    """PID with the largest VmRSS, ignoring skip and pid<=1."""
    best_pid, best_rss = None, 0
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1 or pid in skip:
            continue
        try:
            with open(f"/proc/{pid}/status") as f:
                rss = 0
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) * 1024
                        break
        except (OSError, ValueError):
            continue
        if rss > best_rss:
            best_pid, best_rss = pid, rss
    return best_pid, best_rss


def cmdline(pid):
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read()
    except OSError:
        return "?"
    return raw.replace(b"\0", b" ").decode(errors="replace").strip() or "?"


def check_once(limit, skip):
    used, total = usage()
    if not total:
        return
    ratio = used / total
    if ratio < limit:
        return
    pid, rss = top_process(skip)
    if pid is None:
        return
    desc = cmdline(pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as exc:
        print(f"mybox memguard: cannot kill pid {pid}: {exc}", file=sys.stderr, flush=True)
        return
    print(f"mybox memguard: mem {ratio:.0%} >= {limit:.0%}, "
          f"killed pid {pid} (rss {rss >> 20} MiB) {desc}", file=sys.stderr, flush=True)


def run(limit):
    skip = {os.getpid(), os.getppid()}
    while True:
        time.sleep(INTERVAL)
        check_once(limit, skip)
