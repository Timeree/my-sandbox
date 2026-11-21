import os
import socket
import subprocess
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import ws

HERE = os.path.dirname(os.path.abspath(__file__))
FTP_ROOT = os.environ.get("FTP_ROOT") or os.path.join(HERE, "ftproot")
os.makedirs(FTP_ROOT, exist_ok=True)
SERVICES = {
    21: lambda: subprocess.Popen(
        ["python3", os.path.join(HERE, "ftpd.py"), FTP_ROOT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ),
    22: lambda: subprocess.run(
        ["/usr/sbin/sshd", "-f", "/etc/ssh/sshd_config"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    ),
}
start_lock = threading.Lock()
state_lock = threading.Lock()
state = {"active": 0, "total": 0}


def close_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def probe(port, timeout=1):
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def ensure(port):
    starter = SERVICES.get(port)
    if starter is None:
        return True
    if probe(port):
        return True
    with start_lock:
        if probe(port):
            return True
        if port == 22:
            try:
                os.makedirs("/run/sshd", exist_ok=True)
            except OSError:
                pass
        try:
            starter()
        except (OSError, subprocess.SubprocessError):
            pass
    for _ in range(50):
        if probe(port, timeout=0.2):
            return True
        time.sleep(0.1)
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rbufsize = 0

    def do_GET(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            self.page()
            return
        try:
            port = int(parse_qs(urlparse(self.path).query).get("p", ["22"])[0])
        except ValueError:
            self.send_error(400, "invalid port")
            return
        self.tunnel(port, key)

    def tunnel(self, port, key):
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
        self.end_headers()

        with state_lock:
            state["active"] += 1
            state["total"] += 1

        upstream = None
        done = threading.Event()
        lock = threading.Lock()
        try:
            if not ensure(port):
                return
            upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
            upstream.settimeout(None)
            self.connection.settimeout(None)

            def pump():
                try:
                    while not done.is_set():
                        data = upstream.recv(65536)
                        if not data:
                            break
                        with lock:
                            ws.send_frame(self.connection, 0x2, data, mask=False)
                except OSError:
                    pass
                finally:
                    done.set()
                    close_socket(self.connection)

            threading.Thread(target=pump, daemon=True).start()
            while not done.is_set():
                opcode, payload = ws.recv_frame(self.connection)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    with lock:
                        ws.send_frame(self.connection, 0xA, payload, mask=False)
                elif opcode == 0x2:
                    upstream.sendall(payload)
        except (OSError, EOFError, ValueError):
            pass
        finally:
            done.set()
            with state_lock:
                state["active"] -= 1
            if upstream:
                close_socket(upstream)

    def page(self):
        ssh_ok = ensure(22)
        ftp_ok = ensure(21)
        with state_lock:
            active = state["active"]
            total = state["total"]

        def row(name, ok):
            color = "#34d399" if ok else "#f87171"
            text = "online" if ok else "offline"
            return (
                f'<div class="row"><span>{name}</span>'
                f'<span class="state"><i style="background:{color}"></i>{text}</span></div>'
            )

        body = (
            row("ssh : 22", ssh_ok)
            + row("ftp : 21", ftp_ok)
            + f'<div class="row"><span>active</span><b>{active}</b></div>'
            + f'<div class="row"><span>total</span><b>{total}</b></div>'
            + f'<div class="row"><span>checked</span><span class="muted">{datetime.now():%Y-%m-%d %H:%M:%S}</span></div>'
        )
        html = (
            '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="5">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>my-sandbox</title><style>"
            "*{box-sizing:border-box}"
            "body{margin:0;min-height:100vh;display:grid;place-items:center;"
            "background:#07080c;color:#e6e8ee;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}"
            "main{width:min(420px,90vw)}"
            "h1{margin:0 0 26px;font-size:13px;font-weight:600;letter-spacing:.34em;"
            "text-transform:uppercase;color:#f5f6f8}"
            ".card{border:1px solid #1b1f2a;border-radius:14px;background:#0e1118;padding:4px 20px}"
            ".row{display:flex;align-items:center;justify-content:space-between;"
            "padding:13px 0;border-bottom:1px solid #1b1f2a}"
            ".row:last-child{border-bottom:0}"
            ".row>span:first-child{color:#787f90}"
            ".state{display:flex;align-items:center}"
            ".state i{width:6px;height:6px;border-radius:50%;margin-right:8px}"
            "b{color:#f5f6f8}"
            ".muted{color:#4c5260}"
            "footer{margin-top:16px;font-size:11px;color:#3f4552}"
            "</style></head><body><main><h1>my-sandbox</h1>"
            f'<div class="card">{body}</div>'
            "<footer>auto refresh 5s</footer></main></body></html>"
        )
        payload = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 3000))), Handler).serve_forever()
