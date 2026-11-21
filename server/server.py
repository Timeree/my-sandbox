import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rbufsize = 0

    def do_GET(self):
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            self.send_error(404)
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
            if upstream:
                close_socket(upstream)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 3000))), Handler).serve_forever()
