import argparse
import base64
import os
import subprocess
import sys
import tempfile

from . import __version__, boxes, ftpd, serve, tunnel

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


def askpass(password):
    fd, path = tempfile.mkstemp(prefix="mybox-askpass-")
    blob = base64.b64encode(password.encode()).decode()
    with os.fdopen(fd, "w") as f:
        f.write(f"#!/bin/sh\nprintf %s '{blob}' | base64 -d\n")
    os.chmod(path, 0o700)
    return path


def cmd_ftp(args):
    print(f"mybox ftp: 0.0.0.0:{args.port} root / no password", flush=True)
    try:
        ftpd.serve(args.port, "/")
    except KeyboardInterrupt:
        pass
    return 0


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
    rc = subprocess.call([sys.executable, "-m", "pip", "install", "--upgrade", REPO])
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

    box = tunnel.Tunnel(store[name]["url"])
    if not box.wait_ready(20):
        box.close()
        sys.exit(f"cannot reach {name}: {store[name]['url']}")

    ssh_port = box.forward(args.remote_port, args.local_port)
    for remote, local, ftp in forwards:
        try:
            box.forward(remote, local, ftp)
        except OSError as exc:
            box.close()
            sys.exit(f"cannot listen on 127.0.0.1:{local}: {exc}")

    script = askpass(args.password)
    env = dict(
        os.environ,
        SSH_ASKPASS=script,
        SSH_ASKPASS_REQUIRE="force",
        DISPLAY=os.environ.get("DISPLAY", ":0"),
    )
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
        f"{args.user}@127.0.0.1",
    ]
    try:
        return subprocess.call(cmd, env=env)
    finally:
        os.unlink(script)
        box.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mybox", description="Tunnel SSH over one WebSocket.")
    parser.add_argument("--version", action="version", version=f"mybox {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the server side")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 3000)))
    p.add_argument("--ssh-port", type=int, default=int(os.environ.get("SSH_PORT", 22)))
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("ftp", help="run a no-auth full-access ftp server on this host")
    p.add_argument("port", nargs="?", type=int, default=2121, help="listen port, 2121 by default")
    p.set_defaults(func=cmd_ftp)

    p = sub.add_parser("add", help="remember a box url")
    p.add_argument("items", nargs="+", metavar="[name] url")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("ssh", help="open a shell on a box")
    p.add_argument("name", nargs="?", help="box name, defaults to the first one")
    p.add_argument("-f", "--forward", action="append", default=[], metavar="REMOTE:LOCAL",
                   help="also forward remote port to local port, repeatable")
    p.add_argument("--remote-port", type=int, default=22, help="sshd port on the box")
    p.add_argument("--local-port", type=int, default=0, help="local port for ssh, random by default")
    p.add_argument("--user", default=os.environ.get("MYBOX_USER", "root"))
    p.add_argument("--password", default=os.environ.get("MYBOX_PASS", "myboxpass"))
    p.set_defaults(func=cmd_ssh)

    p = sub.add_parser("ls", help="list boxes")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("update", help="upgrade mybox from the git repo")
    p.set_defaults(func=cmd_update)

    args = parser.parse_args(argv)
    return args.func(args)
