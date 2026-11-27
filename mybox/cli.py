import argparse
import base64
import os
import socket
import subprocess
import sys
import tempfile

from . import __version__, boxes, serve, tunnel

REPO = "git+https://github.com/Timeree/my-sandbox.git@main"


def parse_forwards(items):
    out = []
    for item in items:
        for part in item.split(","):
            part = part.strip()
            if not part:
                continue
            ftp = None
            if part.startswith("ftp:"):
                part, ftp = part[4:], True
            remote, sep, local = part.partition(":")
            if not sep or not remote.isdigit() or not local.isdigit():
                raise ValueError(f"invalid forward: {part}")
            out.append((int(remote), int(local), ftp))
    return out


SSH_OPTS_WITH_ARG = set("BbcDEeFIiJLlmOopQRSWw")


def split_ssh_args(items):
    """把 `--` 之后的参数拆成 ssh 选项和远端命令。

    ssh 的选项必须在 destination 之前，命令必须在之后，所以要分开。
    """
    opts = []
    i = 0
    while i < len(items):
        item = items[i]
        if item == "--":
            return opts, items[i + 1:]
        if not item.startswith("-") or item == "-":
            return opts, items[i:]
        opts.append(item)
        if len(item) == 2 and item[1] in SSH_OPTS_WITH_ARG and i + 1 < len(items):
            i += 1
            opts.append(items[i])
        i += 1
    return opts, []


def askpass(password):
    fd, path = tempfile.mkstemp(prefix="mybox-askpass-")
    blob = base64.b64encode(password.encode()).decode()
    with os.fdopen(fd, "w") as f:
        f.write(f"#!/bin/sh\nprintf %s '{blob}' | base64 -d\n")
    os.chmod(path, 0o700)
    return path


def cmd_serve(args):
    serve.run(args)
    return 0


def cmd_add(args):
    if len(args.items) == 1:
        name, url = None, args.items[0]
    elif len(args.items) == 2:
        name, url = args.items
    else:
        sys.exit("usage: mybox add [name] <url>")

    url = boxes.normalize(url)
    store = boxes.load()
    if name is None:
        name = boxes.next_name(store)
    elif name in store:
        sys.exit(f"box already exists: {name}")

    print(f"checking {url} ...", flush=True)
    try:
        boxes.probe(url)
    except Exception as exc:
        sys.exit(f"unreachable: {exc}")

    store[name] = {"url": url}
    boxes.save(store)
    print(f"{name} -> {url}")
    return 0


def cmd_update(args):
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade",
                          "--force-reinstall", "--no-deps", REPO])
    if rc:
        print("update failed, check the network and retry", file=sys.stderr)
    return rc


def cmd_ls(args):
    store = boxes.load()
    if not store:
        print("no box yet: mybox add <url>")
        return 0
    width = max(len(name) for name in store)
    for name, item in store.items():
        print(f"{name.ljust(width)}  {item['url']}")
    return 0


def cmd_remove(args):
    store = boxes.load()
    name = args.name or next(iter(store), None)
    if name is None:
        sys.exit("no box yet: mybox add <url>")
    if name not in store:
        sys.exit(f"no such box: {name}")
    url = store.pop(name)["url"]
    boxes.save(store)
    print(f"removed {name}  {url}")
    return 0


def cmd_rename(args):
    if len(args.items) == 1:
        old, new = None, args.items[0]
    elif len(args.items) == 2:
        old, new = args.items
    else:
        sys.exit("usage: mybox rename [old] new")

    store = boxes.load()
    old = old or next(iter(store), None)
    if old is None:
        sys.exit("no box yet: mybox add <url>")
    if old not in store:
        sys.exit(f"no such box: {old}")
    if new in store:
        sys.exit(f"box already exists: {new}")
    url = store[old]["url"]
    store = {new if k == old else k: v for k, v in store.items()}
    boxes.save(store)
    print(f"{old} -> {new}  {url}")
    return 0


def log(*parts):
    print(f"[mybox] {' '.join(str(p) for p in parts)}", file=sys.stderr, flush=True)


def probe_ssh(port, timeout=10):
    """Read the SSH banner through the tunnel; return (ok, detail)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            banner = sock.recv(256)
    except (OSError, socket.timeout) as exc:
        return False, str(exc) or type(exc).__name__
    if not banner:
        return False, "connection closed before SSH banner"
    if not banner.startswith(b"SSH-"):
        return False, f"unexpected banner: {banner[:60]!r}"
    return True, banner.decode(errors="replace").strip()


def cmd_ssh(args):
    store = boxes.load()
    name = args.name or next(iter(store), None)
    if name is None:
        sys.exit("no box yet: mybox add <url>")
    if name not in store:
        sys.exit(f"no such box: {name}")

    try:
        forwards = parse_forwards(args.forward)
    except ValueError as exc:
        sys.exit(str(exc))
    if args.ftp is not None:
        forwards.append((2121, args.ftp, True))

    url = store[name]["url"]
    log(f"connecting to {name}: {url}")
    box = tunnel.Tunnel(url)
    if not box.wait_ready(20):
        box.close()
        sys.exit(f"cannot reach {name}: {url}")
    log("tunnel ready")

    ssh_port = box.forward(args.remote_port, args.local_port)
    for remote, local, ftp in forwards:
        try:
            box.forward(remote, local, ftp)
        except OSError as exc:
            box.close()
            sys.exit(f"cannot listen on 127.0.0.1:{local}: {exc}")
    log(f"listening: ssh 127.0.0.1:{ssh_port}" +
        ("".join(f", ftp {local}" for _, local, ftp in forwards if ftp) or ""))

    ok, detail = probe_ssh(ssh_port)
    if not ok:
        box.close()
        sys.exit(f"sshd on the box did not answer (remote port {args.remote_port}): {detail}")
    log(f"sshd ok: {detail}")

    script = askpass(args.password)
    env = dict(
        os.environ,
        SSH_ASKPASS=script,
        SSH_ASKPASS_REQUIRE="force",
        DISPLAY=os.environ.get("DISPLAY", ":0"),
    )
    opts, command = split_ssh_args(args.extra)
    cmd = [
        "ssh",
        "-p", str(ssh_port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "PubkeyAuthentication=no",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "NumberOfPasswordPrompts=1",
        *opts,
        f"{args.user}@127.0.0.1",
        *command,
    ]
    try:
        return subprocess.call(cmd, env=env)
    finally:
        os.unlink(script)
        box.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        cut = argv.index("--")
        argv, extra = argv[:cut], argv[cut + 1:]

    parser = argparse.ArgumentParser(prog="mybox", description="Tunnel SSH over one WebSocket.")
    parser.add_argument("--version", action="version", version=f"mybox {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the server side")
    p.add_argument("-p", "--password", nargs="?", const=True, default=None,
                   help="require ?p= on the handshake; omit value to generate a random one")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 3000)))
    p.add_argument("--ssh-port", type=int, default=int(os.environ.get("SSH_PORT", 22)))
    p.add_argument("--ftp-port", type=int, default=int(os.environ.get("FTP_PORT", 2121)))
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("add", help="remember a box url")
    p.add_argument("items", nargs="+", metavar="[name] url")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser(
        "ssh", help="open a shell on a box",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "arguments after -- go to ssh verbatim:\n"
            "  mybox ssh -- ls -l /var/log\n"
            "  mybox ssh box1 -- -t 'cd /app && ls; exec bash'\n"
            "  mybox ssh box1 -- -L 9090:localhost:80 -N\n"
            "  mybox ssh box1 -- -f -L 9090:localhost:80   (ssh's own -f)\n"
        ),
    )
    p.add_argument("name", nargs="?", help="box name, defaults to the first one")
    p.add_argument("-f", "--forward", action="append", default=[], metavar="REMOTE:LOCAL",
                   help="also forward remote port to local port, repeatable")
    p.add_argument("--ftp", type=int, metavar="LOCAL_PORT",
                   help="map the box's ftp (2121) to this local port")
    p.add_argument("--remote-port", type=int, default=22, help="sshd port on the box")
    p.add_argument("--local-port", type=int, default=0, help="local port for ssh, random by default")
    p.add_argument("--user", default=os.environ.get("MYBOX_USER", "root"))
    p.add_argument("--password", default=os.environ.get("MYBOX_PASS", "myboxpass"))
    p.set_defaults(func=cmd_ssh)

    p = sub.add_parser("ls", help="list boxes")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("remove", help="remove a remembered box")
    p.add_argument("name", nargs="?", help="box name, defaults to the first one")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("rename", help="rename a remembered box")
    p.add_argument("items", nargs="+", metavar="[old] new")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("update", help="upgrade mybox from the git repo")
    p.set_defaults(func=cmd_update)

    args = parser.parse_args(argv)
    args.extra = extra
    return args.func(args)
