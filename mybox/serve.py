import http.client
import os
import queue
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from . import ftpd, wsproto

SSHD_CONFIG = "/tmp/mybox-sshd_config"
PATHS = {"/my-sandbox"}
BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
PROXY_PREFIX = "/my-sandbox"
CONTROL_PARAMS = {"port", "p"}
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}

DATA, OPEN, FIN, RESET = 0, 1, 2, 3
HDR = struct.Struct("!IB")
PING_INTERVAL = 15
QMAX = 128
SHUTDOWN = object()
ABORT = object()


def start_sshd(port):
    conf = (
        f"Port {port}\n"
        "PermitRootLogin yes\n"
        "PasswordAuthentication yes\n"
        "UsePAM no\n"
    )
    with open(SSHD_CONFIG, "w") as f:
        f.write(conf)
    subprocess.run(
        ["/usr/sbin/sshd", "-f", SSHD_CONFIG],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


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


def random_password(n=8):
    return "".join(secrets.choice(BASE32) for _ in range(n))


def check_password(path, password):
    if not password:
        return True
    query = parse_qs(urlparse(path).query)
    return query.get("p", [None])[0] == password


class Mux:
    def __init__(self, conn, services):
        self.conn = conn
        self.services = services
        self.wlock = threading.Lock()
        self.lock = threading.Lock()
        self.streams = {}
        self.stopped = False
        self.start_lock = threading.Lock()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _keepalive(self):
        while not self.stopped:
            time.sleep(PING_INTERVAL)
            if self.stopped:
                break
            try:
                with self.wlock:
                    wsproto.send_frame(self.conn, 0x9, b"", mask=False)
            except (OSError, EOFError):
                break

    def send(self, sid, kind, data=b""):
        try:
            with self.wlock:
                wsproto.send_frame(self.conn, 0x2, HDR.pack(sid, kind) + data, mask=False)
        except (OSError, EOFError):
            pass

    def ensure(self, port):
        starter = self.services.get(port)
        if starter is None:
            return True
        if probe(port):
            return True
        with self.start_lock:
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

    def open(self, sid, port):
        entry = {
            "sock": None, "outq": queue.Queue(QMAX), "cl_fin": False, "up_fin": False,
            "lock": threading.Lock(), "closed": False, "w_done": False,
        }
        with self.lock:
            self.streams[sid] = entry
        threading.Thread(target=self._connect, args=(sid, entry, port), daemon=True).start()

    def _connect(self, sid, entry, port):
        sock = None
        try:
            if self.ensure(port):
                sock = socket.create_connection(("127.0.0.1", port), timeout=10)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(None)
        except OSError:
            sock = None
        if sock is None:
            self.send(sid, RESET)
            with self.lock:
                self.streams.pop(sid, None)
            with entry["lock"]:
                entry["closed"] = True
            return
        with entry["lock"]:
            entry["sock"] = sock
        threading.Thread(target=self._writer, args=(sid, entry, sock), daemon=True).start()
        threading.Thread(target=self._pump, args=(sid, sock), daemon=True).start()

    def _writer(self, sid, entry, sock):
        outq = entry["outq"]
        try:
            while True:
                item = outq.get()
                if item is ABORT:
                    return
                if item is SHUTDOWN:
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    return
                sock.sendall(item)
        except OSError:
            self.send(sid, RESET)
            self._finish(sid, cl=True, up=True)
        finally:
            with entry["lock"]:
                entry["w_done"] = True
                ready = entry["closed"]
            if ready:
                close_socket(sock)

    def _pump(self, sid, sock):
        try:
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                self.send(sid, DATA, data)
        except OSError:
            pass
        finally:
            self.send(sid, FIN)
            self._finish(sid, up=True)

    def feed(self, sid, body):
        with self.lock:
            entry = self.streams.get(sid)
        if not entry:
            return
        # 队列满时背压等待而非 RESET，避免突发上传误断流
        while True:
            with entry["lock"]:
                if entry["closed"]:
                    return
            try:
                entry["outq"].put(body, timeout=0.5)
                return
            except queue.Full:
                continue

    def fin(self, sid):
        with self.lock:
            entry = self.streams.get(sid)
        if not entry:
            return
        while True:
            with entry["lock"]:
                if entry["closed"]:
                    return
            try:
                entry["outq"].put(SHUTDOWN, timeout=0.5)
                break
            except queue.Full:
                continue
        self._finish(sid, cl=True)

    def reset(self, sid):
        with self.lock:
            entry = self.streams.pop(sid, None)
        if entry:
            with entry["lock"]:
                entry["closed"] = True
                sock = entry["sock"]
            try:
                entry["outq"].put_nowait(ABORT)
            except queue.Full:
                pass
            if sock:
                close_socket(sock)

    def _finish(self, sid, cl=False, up=False):
        with self.lock:
            entry = self.streams.get(sid)
            if not entry:
                return
        with entry["lock"]:
            entry["cl_fin"] |= cl
            entry["up_fin"] |= up
            done = entry["cl_fin"] and entry["up_fin"]
            sock = entry["sock"]
            ready = entry["w_done"]
            if done:
                entry["closed"] = True
        if done:
            with self.lock:
                self.streams.pop(sid, None)
            if ready and sock:
                close_socket(sock)

    def run(self):
        try:
            while True:
                opcode, payload = wsproto.recv_frame(self.conn)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    with self.wlock:
                        wsproto.send_frame(self.conn, 0xA, payload, mask=False)
                    continue
                if opcode != 0x2 or len(payload) < HDR.size:
                    continue
                sid, kind = HDR.unpack(payload[:HDR.size])
                body = payload[HDR.size:]
                if kind == OPEN:
                    self.open(sid, struct.unpack("!H", body[:2])[0])
                elif kind == DATA:
                    self.feed(sid, body)
                elif kind == FIN:
                    self.fin(sid)
                elif kind == RESET:
                    self.reset(sid)
        except (OSError, EOFError, ValueError):
            pass
        finally:
            self.stopped = True
            with self.lock:
                entries = list(self.streams.values())
                self.streams.clear()
            for entry in entries:
                with entry["lock"]:
                    entry["closed"] = True
                    sock = entry["sock"]
                try:
                    entry["outq"].put_nowait(ABORT)
                except queue.Full:
                    pass
                if sock:
                    close_socket(sock)
            wsproto.drain(self.conn)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rbufsize = 0

    def do_GET(self):
        if self._is_websocket():
            self._tunnel()
        else:
            self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_HEAD(self):
        self._proxy()

    def do_OPTIONS(self):
        self._proxy()

    def _is_websocket(self):
        return (self.headers.get("Sec-WebSocket-Key")
                and self.headers.get("Upgrade", "").lower() == "websocket")

    def _tunnel(self):
        key = self.headers["Sec-WebSocket-Key"]
        if self.path.split("?", 1)[0] not in PATHS:
            self.send_error(404)
            return
        if not check_password(self.path, self.server.password):
            self.send_error(401)
            return
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        wsproto.keepalive(self.connection)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", wsproto.accept_key(key))
        self.end_headers()
        Mux(self.connection, self.server.services).run()

    def _cookie(self, name):
        for item in self.headers.get("Cookie", "").split(";"):
            k, _, v = item.strip().partition("=")
            if k == name:
                return v
        return None

    def _proxy(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        rel = (parsed.path[len(PROXY_PREFIX):]
               if parsed.path.startswith(PROXY_PREFIX) else "")

        port = None
        base = parsed.path
        if rel.startswith("/"):
            head, sep, tail = rel[1:].partition("/")
            if head:
                if not head.isdigit():
                    self.send_error(400, "bad port")
                    return
                port = head
                base = "/" + tail if sep else "/"
        if port is None:
            port = self._cookie("mybox_port")
        if not port:
            self.send_error(404, "missing port path")
            return
        if not check_password(self.path, self.server.password):
            self.send_error(401)
            return
        try:
            port = int(port)
        except (TypeError, ValueError):
            self.send_error(400, "bad port")
            return
        if not 1 <= port <= 65535:
            self.send_error(400, "bad port")
            return

        forward = {k: v for k, v in query.items() if k not in CONTROL_PARAMS}
        target = base + ("?" + urlencode(forward, doseq=True) if forward else "")

        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_HEADERS}
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
            conn.request(self.command, target, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            self.send_response(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            self.send_header("Set-Cookie", f"mybox_port={port}; Path=/; Max-Age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            conn.close()
        except (OSError, http.client.HTTPException) as exc:
            try:
                self.send_error(502, f"upstream error: {exc}")
            except (OSError, ValueError):
                pass

    def log_message(self, *args):
        pass


def start_ftp(port):
    try:
        ftpd.serve(port, "/")
    except OSError as exc:
        print(f"mybox serve: ftp {port} unavailable: {exc}", file=sys.stderr, flush=True)


def run(args):
    password = args.password
    if password is True:
        password = random_password()
    services = {args.ssh_port: lambda: start_sshd(args.ssh_port)}
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.services = services
    server.password = password
    threading.Thread(target=start_ftp, args=(args.ftp_port,), daemon=True).start()
    info = f"mybox serve: http {args.port}, ssh {args.ssh_port}, ftp {args.ftp_port}"
    if password:
        info += f", password {password}"
    print(info, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
