import argparse
import base64
import hashlib
import os
import re
import socket
import ssl
import struct
import threading
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PASV = re.compile(rb"(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")
EPSV = re.compile(rb"\(\|\|\|(\d+)\|\)")
DATA_TIMEOUT = 30


def send_frame(sock, opcode, payload, mask=True):
    size = len(payload)
    header = bytes([0x80 | opcode])
    if size < 126:
        header += bytes([0x80 | size] if mask else [size])
    elif size < 65536:
        header += bytes([0x80 | 126] if mask else [126]) + struct.pack("!H", size)
    else:
        header += bytes([0x80 | 127] if mask else [127]) + struct.pack("!Q", size)
    if not mask:
        sock.sendall(header + payload)
        return
    key = os.urandom(4)
    body = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    sock.sendall(header + key + body)


def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def recv_frame(sock):
    first, second = recv_exact(sock, 2)
    opcode = first & 0x0F
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", recv_exact(sock, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", recv_exact(sock, 8))[0]
    if second & 0x80:
        key = recv_exact(sock, 4)
        payload = bytes(byte ^ key[i % 4] for i, byte in enumerate(recv_exact(sock, size)))
    else:
        payload = recv_exact(sock, size) if size else b""
    return opcode, payload


def connect_ws(host, port, path, tls):
    sock = socket.create_connection((host, port), timeout=15)
    if tls:
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode())
    header = b""
    while b"\r\n\r\n" not in header:
        header += recv_exact(sock, 1)
    if b"101" not in header.split(b"\r\n")[0]:
        raise RuntimeError(header.decode(errors="replace"))
    return sock


def close_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class Tunnel:
    def __init__(self, url):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https", "ws", "wss"):
            raise ValueError(f"unsupported scheme: {parsed.scheme}")
        if not parsed.hostname:
            raise ValueError("missing host")
        self.host = parsed.hostname
        self.tls = parsed.scheme in ("https", "wss")
        self.port = parsed.port or (443 if self.tls else 80)
        self.path = parsed.path or "/"
        self.query = parsed.query

    def open(self, remote):
        query = (self.query + "&" if self.query else "") + f"p={remote}"
        return connect_ws(self.host, self.port, f"{self.path}?{query}", self.tls)


def relay(local, upstream):
    done = threading.Event()
    lock = threading.Lock()

    def to_ws():
        try:
            while not done.is_set():
                data = local.recv(65536)
                if not data:
                    break
                with lock:
                    send_frame(upstream, 0x2, data)
        except (OSError, EOFError):
            pass
        finally:
            done.set()
            close_socket(upstream)

    def to_local():
        try:
            while not done.is_set():
                opcode, payload = recv_frame(upstream)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    with lock:
                        send_frame(upstream, 0xA, payload)
                elif opcode == 0x2:
                    local.sendall(payload)
        except (OSError, EOFError):
            pass
        finally:
            done.set()
            close_socket(local)

    threading.Thread(target=to_ws, daemon=True).start()
    threading.Thread(target=to_local, daemon=True).start()
    done.wait()
    close_socket(local)
    close_socket(upstream)


def pipe(local, tunnel, remote):
    try:
        relay(local, tunnel.open(remote))
    except (OSError, EOFError, RuntimeError):
        close_socket(local)


def data_lane(tunnel, remote):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(DATA_TIMEOUT)
    port = listener.getsockname()[1]

    def run():
        try:
            conn, _ = listener.accept()
            conn.settimeout(None)
            relay(conn, tunnel.open(remote))
        except (OSError, EOFError, RuntimeError):
            pass
        finally:
            listener.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def rewrite(line, tunnel):
    if line.startswith(b"227"):
        match = PASV.search(line)
        if match:
            remote = int(match.group(5)) * 256 + int(match.group(6))
            local = data_lane(tunnel, remote)
            return line[: match.start()] + f"127,0,0,1,{local // 256},{local % 256}".encode() + line[match.end() :]
    elif line.startswith(b"229"):
        match = EPSV.search(line)
        if match:
            remote = int(match.group(1))
            local = data_lane(tunnel, remote)
            return line[: match.start()] + f"(|||{local}|)".encode() + line[match.end() :]
    return line


def ftp_session(local, tunnel):
    try:
        upstream = tunnel.open(21)
    except (OSError, EOFError, RuntimeError):
        close_socket(local)
        return

    done = threading.Event()
    lock = threading.Lock()
    buffer = b""

    def to_ws():
        try:
            while not done.is_set():
                data = local.recv(65536)
                if not data:
                    break
                with lock:
                    send_frame(upstream, 0x2, data)
        except (OSError, EOFError):
            pass
        finally:
            done.set()
            close_socket(upstream)

    def to_local():
        nonlocal buffer
        try:
            while not done.is_set():
                opcode, payload = recv_frame(upstream)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    with lock:
                        send_frame(upstream, 0xA, payload)
                    continue
                if opcode != 0x2:
                    continue
                buffer += payload
                while b"\r\n" in buffer:
                    line, buffer = buffer.split(b"\r\n", 1)
                    local.sendall(rewrite(line, tunnel) + b"\r\n")
        except (OSError, EOFError):
            pass
        finally:
            done.set()
            close_socket(local)

    threading.Thread(target=to_ws, daemon=True).start()
    threading.Thread(target=to_local, daemon=True).start()
    done.wait()
    close_socket(local)
    close_socket(upstream)


def serve(port, handler):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(16)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=handler, args=(conn,), daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description="my-sandbox client")
    parser.add_argument("--url", required=True, help="tunnel URL, e.g. https://host/?sharecode=XXX")
    parser.add_argument("--ssh", type=int, metavar="PORT", help="expose remote ssh on local PORT")
    parser.add_argument("--ftp", type=int, metavar="PORT", help="expose remote ftp on local PORT")
    parser.add_argument(
        "--forward", action="append", default=[], metavar="REMOTE:LOCAL",
        help="forward remote port to local port, repeatable",
    )
    args = parser.parse_args()

    forwards = []
    for item in args.forward:
        try:
            remote, local = (int(part) for part in item.split(":"))
        except ValueError:
            parser.error(f"invalid --forward value: {item}")
        forwards.append((remote, local, "tcp"))
    if args.ssh:
        forwards.append((22, args.ssh, "tcp"))
    if args.ftp:
        forwards.append((21, args.ftp, "ftp"))
    if not forwards:
        parser.error("nothing to forward: use --ssh, --ftp or --forward")

    tunnel = Tunnel(args.url)
    for remote, local, kind in forwards:
        if kind == "ftp":
            handler = lambda conn, t=tunnel: ftp_session(conn, t)
        else:
            handler = lambda conn, r=remote, t=tunnel: pipe(conn, t, r)
        threading.Thread(target=serve, args=(local, handler), daemon=True).start()
        print(f"127.0.0.1:{local} -> remote:{remote}")

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
