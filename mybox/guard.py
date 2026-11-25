"""采样内存/连接/进程，异常时落盘现场，可选按名字精确清理。"""
import os
import signal
import subprocess
import time
from collections import deque
from pathlib import Path

LOG = Path(os.environ.get("MYBOX_GUARD_LOG") or Path.home() / ".config" / "mybox" / "guard.log")
CG = Path("/sys/fs/cgroup")
HARD = ("docker-init", "supervisord", "sandbox-proxy", "editor_sdk", "sshd", "mybox")


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


def kill_named(name):
    """只杀命令行里含 name 且不在 HARD 白名单里的进程。"""
    killed = []
    for line in shell("ps -eo pid=,args=").splitlines():
        line = line.strip()
        if not line or name not in line:
            continue
        pid, _, args = line.partition(" ")
        if any(h in args for h in HARD) or int(pid) <= 1:
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed.append(f"{pid}:{args[:70]}")
        except (OSError, ValueError):
            pass
    return killed


def run(args):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    ring = deque(maxlen=args.keep)
    over = 0
    last_oom = None
    last_dump = 0
    print(f"mybox guard: log {LOG}, every {args.interval}s, "
          f"kill={'off' if not args.kill else args.kill}", flush=True)
    while True:
        s = sample()
        ring.append(s)
        LOG.open("a").write(render(s) + "\n")
        cause = []
        if s["dstate"]:
            cause.append(f"D-state x{len(s['dstate'])}")
        if last_oom is not None and int(s["oom_kill"]) > int(last_oom):
            cause.append("oom_kill increased")
        if s["pct"] >= args.high:
            over += 1
            cause.append(f"mem>={args.high}% x{over}")
        else:
            over = 0
        last_oom = s["oom_kill"]
        if cause and time.time() - last_dump >= args.cooldown:
            dump(ring, ", ".join(cause))
            last_dump = time.time()
        if args.kill and over >= args.strikes:
            killed = kill_named(args.kill)
            if killed:
                LOG.open("a").write(f"killed: {', '.join(killed)}\n")
            over = 0
        time.sleep(args.interval)
