import os
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ws

HERE = os.path.dirname(os.path.abspath(__file__))
FTP_ROOT = os.environ.get("FTP_ROOT") or "/"
os.makedirs(FTP_ROOT, exist_ok=True)
SSHD_CONFIG = "/tmp/mybox-sshd_config"
SSHD_CONF = """Port 22
PermitRootLogin yes
PasswordAuthentication yes
UsePAM no
ClientAliveInterval 10
ClientAliveCountMax 3
"""

DATA, OPEN, FIN, RESET = 0, 1, 2, 3
HDR = struct.Struct("!IB")


def start_sshd():
    with open(SSHD_CONFIG, "w") as f:
        f.write(SSHD_CONF)
    subprocess.run(
        ["/usr/sbin/sshd", "-f", SSHD_CONFIG],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


SERVICES = {
    21: lambda: subprocess.Popen(
        ["python3", os.path.join(HERE, "ftpd.py"), FTP_ROOT],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ),
    22: start_sshd,
}
start_lock = threading.Lock()


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


class Mux:
    def __init__(self, conn):
        self.conn = conn
        self.wlock = threading.Lock()
        self.lock = threading.Lock()
        self.streams = {}

    def send(self, sid, kind, data=b""):
        try:
            with self.wlock:
                ws.send_frame(self.conn, 0x2, HDR.pack(sid, kind) + data, mask=False)
        except (OSError, EOFError):
            pass

    def open(self, sid, port):
        entry = {
            "sock": None, "queue": [], "cl_fin": False, "up_fin": False,
            "lock": threading.Lock(), "closed": False,
        }
        with self.lock:
            self.streams[sid] = entry
        threading.Thread(target=self._connect, args=(sid, entry, port), daemon=True).start()

    def _connect(self, sid, entry, port):
        sock = None
        try:
            if ensure(port):
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
                entry["queue"].clear()
            return
        with entry["lock"]:
            entry["sock"] = sock
            pending = entry["queue"]
            entry["queue"] = []
            cl_fin = entry["cl_fin"]
        try:
            for chunk in pending:
                sock.sendall(chunk)
            if cl_fin:
                sock.shutdown(socket.SHUT_WR)
        except OSError:
            self.send(sid, RESET)
            self._finish(sid, cl=True, up=True)
            return
        threading.Thread(target=self._pump, args=(sid, sock), daemon=True).start()

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
        with entry["lock"]:
            if entry["closed"]:
                return
            if entry["sock"] is None:
                entry["queue"].append(body)
                return
            sock = entry["sock"]
        try:
            sock.sendall(body)
        except OSError:
            self.send(sid, RESET)
            self._finish(sid, cl=True, up=True)

    def fin(self, sid):
        with self.lock:
            entry = self.streams.get(sid)
        if not entry:
            return
        with entry["lock"]:
            entry["cl_fin"] = True
            sock = entry["sock"]
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        self._finish(sid, cl=True)

    def reset(self, sid):
        with self.lock:
            entry = self.streams.pop(sid, None)
        if entry:
            with entry["lock"]:
                entry["closed"] = True
                entry["queue"].clear()
                sock = entry["sock"]
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
            if done:
                entry["closed"] = True
                entry["queue"].clear()
        if done:
            with self.lock:
                self.streams.pop(sid, None)
            if sock:
                close_socket(sock)

    def run(self):
        try:
            while True:
                opcode, payload = ws.recv_frame(self.conn)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    with self.wlock:
                        ws.send_frame(self.conn, 0xA, payload, mask=False)
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
            with self.lock:
                entries = list(self.streams.values())
                self.streams.clear()
            for entry in entries:
                with entry["lock"]:
                    entry["closed"] = True
                    sock = entry["sock"]
                if sock:
                    close_socket(sock)
            ws.drain(self.conn)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rbufsize = 0

    def do_GET(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            self.send_error(404)
            return
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws.accept_key(key))
        self.end_headers()
        Mux(self.connection).run()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 3000))), Handler).serve_forever()
