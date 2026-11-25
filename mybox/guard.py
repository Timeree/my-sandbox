"""采样内存/连接/进程，异常时落盘现场，超阈值杀最大的用户进程。"""
import os
import signal
import subprocess
import time
from collections import deque
from pathlib import Path

LOG = Path(os.environ.get("MYBOX_GUARD_LOG") or Path.home() / ".config" / "mybox" / "guard.log")
CG = Path("/sys/fs/cgroup")
HARD = (
    "docker-init", "supervisord", "sshd", "mybox",
    "sandbox", "editor_sdk", "codebuddy", "workbuddy",
    ".PlnPyKFp4CRfFtgC1", "PM2", "dockerd", "containerd",
    "sync_server", "envd", "log-forward",
)


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def num(name):
    try:
        return int(read(CG / name))
    except ValueError:
        return 0


def events():
    out = {}
    for line in read(CG / "memory.events").splitlines():
        key, _, value = line.partition(" ")
        out[key] = value
    return out


def shell(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def sample():
    cur = num("memory.current")
    limit = num("memory.max")
    ev = events()
    dstate = [line for line in shell("ps -eo stat,pid,comm").splitlines()
              if line.strip() and line.split()[0].startswith("D")]
    conns = max(0, len(shell("ss -tn state established").splitlines()) - 1)
    return {
        "time": time.strftime("%F %T"),
        "cur": cur,
        "limit": limit,
        "pct": round(cur / limit * 100, 1) if limit else 0,
        "dstate": dstate,
        "conns": conns,
        "oom": ev.get("oom", "0"),
        "oom_kill": ev.get("oom_kill", "0"),
    }


def render(s):
    return (f"{s['time']} mem={s['cur'] // 1048576}M/{s['pct']}% "
            f"oom={s['oom']} oom_kill={s['oom_kill']} D={len(s['dstate'])} conn={s['conns']}")


def top(n=8):
    return shell(f"ps -eo pid,rss,stat,comm --sort=-rss | head -n {n + 1}")


def dump(ring, reason):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    block = [f"===== {time.strftime('%F %T')} TRIGGER: {reason} ====="]
    block += [f"  {render(s)}" for s in ring]
    block += ["--- top rss ---", top()]
    with LOG.open("a") as f:
        f.write("\n".join(block) + "\n\n")


def biggest_user():
    """RSS 最大、且命令行不含 HARD 任一关键词、PID>1 的进程。"""
    best = None
    for line in shell("ps -eo pid=,rss=,args=").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        pid, rss, args = parts
        if not pid.isdigit() or not rss.isdigit() or int(pid) <= 1:
            continue
        if any(h in args for h in HARD):
            continue
        if best is None or int(rss) > best[1]:
            best = (int(pid), int(rss), args)
    return best


def kill_biggest():
    best = biggest_user()
    if not best:
        return None
    pid, rss, args = best
    try:
        os.kill(pid, signal.SIGTERM)
        return f"{pid}:{rss // 1024}M:{args[:60]}"
    except OSError:
        return None


def run(args):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ring = deque(maxlen=args.keep)
    last_oom = None
    last_dump = 0
    last_kill = 0
    cutoff = args.kill_at * 1048576 if args.kill_at else None
    status = f"kill biggest user proc above {args.kill_at}M" if args.kill_at else "kill off"
    print(f"mybox guard: log {LOG}, every {args.interval}s, {status}", flush=True)
    while True:
        s = sample()
        ring.append(s)
        LOG.open("a").write(render(s) + "\n")
        cause = []
        urgent = False
        if s["dstate"]:
            cause.append(f"D-state x{len(s['dstate'])}")
            urgent = True
        if last_oom is not None and int(s["oom_kill"]) > int(last_oom):
            cause.append("oom_kill increased")
            urgent = True
        if s["pct"] >= args.critical:
            cause.append(f"mem>={args.critical}% CRITICAL")
            urgent = True
        if s["pct"] >= args.high:
            cause.append(f"mem>={args.high}%")
        last_oom = s["oom_kill"]
        if cause and (urgent or time.time() - last_dump >= args.cooldown):
            dump(ring, ", ".join(cause))
            last_dump = time.time()
        if cutoff and s["cur"] >= cutoff and time.time() - last_kill >= args.cooldown:
            killed = kill_biggest()
            if killed:
                LOG.open("a").write(f"kill@{s['cur'] // 1048576}M -> {killed}\n")
                dump(ring, f"killed biggest user proc: {killed}")
            last_kill = time.time()
        time.sleep(args.interval)
