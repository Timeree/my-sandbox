import queue
import re
import socket
import struct
import threading
import time
from urllib.parse import urlparse

from . import wsproto

PASV = re.compile(rb"(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")
EPSV = re.compile(rb"\(\|\|\|(\d+)\|\)")
DATA_TIMEOUT = 30
PING_INTERVAL = 15
DATA, OPEN, FIN, RESET = 0, 1, 2, 3
HDR = struct.Struct("!IB")
QMAX = 128
SHUTDOWN = object()
ABORT = object()


def close_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def parse(url):
    """Split a box URL into (host, port, path, tls)."""
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https", "ws", "wss"):
        raise ValueError(f"unsupported scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("missing host")
    tls = parsed.scheme in ("https", "wss")
    port = parsed.port or (443 if tls else 80)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return parsed.hostname, port, path, tls


class Sink:
    def __init__(self, sock):
        self.sock = sock

    def recv(self):
        return self.sock.recv(65536)

    def feed(self, data):
        self.sock.sendall(data)

    def shutdown_wr(self):
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def close(self):
        close_socket(self.sock)


class FtpSink(Sink):
    LOCAL = {
        b"TYPE": b"200 type set\r\n",
        b"OPTS": b"200 ok\r\n",
        b"CLNT": b"200 ok\r\n",
        b"ALLO": b"200 ok\r\n",
        b"SITE": b"200 ok\r\n",
        b"NOOP": b"200 ok\r\n",
    }
    TRANSFER = {b"LIST", b"NLST", b"RETR", b"STOR", b"APPE", b"MLSD"}

    def __init__(self, sock, mux):
        super().__init__(sock)
        self.mux = mux
        self.buf = b""
        self.rbuf = b""
        self.sid = None
        self.cache = None
        self.hidden = 0
        self.used_pasv = False
        self.lock = threading.Lock()
        self.cond = threading.Condition()

    def take_prefetch(self):
        with self.cond:
            if not self.used_pasv or self.cache is not None or self.hidden or self.sid is None:
                return b""
            self.hidden = 1
            return b"PASV\r\n"

    def reply(self, data):
        try:
            with self.lock:
                self.sock.sendall(data)
        except OSError:
            pass

    def recv(self):
        while True:
            while b"\r\n" not in self.rbuf:
                data = self.sock.recv(65536)
                if not data:
                    line, self.rbuf = self.rbuf, b""
                    return line
                self.rbuf += data
            line, self.rbuf = self.rbuf.split(b"\r\n", 1)
            line += b"\r\n"
            head = line.split(b" ", 1)[0].strip().upper()
            if head in (b"PASV", b"EPSV"):
                with self.cond:
                    self.used_pasv = True
                    while self.cache is None and self.hidden:
                        self.cond.wait(5)
                    if self.cache is None and self.hidden:
                        self.hidden -= 1
                    port = self.cache
                    self.cache = None
                if port is not None:
                    local = data_lane(self.mux, port)
                    if head == b"PASV":
                        text = f"227 Entering Passive Mode (127,0,0,1,{local // 256},{local % 256})\r\n"
                    else:
                        text = f"229 Entering Extended Passive Mode (|||{local}|)\r\n"
                    self.reply(text.encode())
                    continue
                return line
            answer = self.LOCAL.get(head)
            if answer is None:
                if head in FtpSink.TRANSFER:
                    with self.cond:
                        self.cache = None
                    # 预取的 PASV 必须跟在传输命令同一帧里发出，服务端按序处理才不会抢掉本次数据端口
                    line += self.take_prefetch()
                return line
            self.reply(answer)

    def feed(self, data):
        self.buf += data
        while b"\r\n" in self.buf:
            line, self.buf = self.buf.split(b"\r\n", 1)
            if line.startswith((b"425", b"426")):
                with self.cond:
                    self.cache = None
            if line.startswith((b"227", b"229")):
                with self.cond:
                    if self.hidden:
                        self.hidden -= 1
                        match = PASV.search(line) if line.startswith(b"227") else None
                        if self.cache is None and match:
                            self.cache = int(match.group(5)) * 256 + int(match.group(6))
                        self.cond.notify_all()
                        continue
            self.reply(rewrite(line, self.mux) + b"\r\n")


class Mux:
    def __init__(self, opener):
        self.opener = opener
        self.sock = None
        self.wlock = threading.Lock()
        self.lock = threading.Lock()
        self.streams = {}
        self.next_id = 0
        self.ready = threading.Event()
        self.stopped = False
        threading.Thread(target=self._supervise, daemon=True).start()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def stop(self):
        self.stopped = True
        sock = self.sock
        if sock is not None:
            close_socket(sock)

    def _supervise(self):
        while not self.stopped:
            try:
                sock = self.opener()
            except (OSError, EOFError, RuntimeError):
                time.sleep(2)
                continue
            if self.stopped:
                close_socket(sock)
                return
            self.sock = sock
            self.ready.set()
            self._reader(sock)
            self.ready.clear()
            with self.lock:
                entries = list(self.streams.values())
                self.streams.clear()
            for entry in entries:
                with entry["lock"]:
                    entry["closed"] = True
                try:
                    entry["outq"].put_nowait(ABORT)
                except queue.Full:
                    pass
                entry["sink"].close()
            self.next_id = 0
            close_socket(sock)
            time.sleep(1)

    def _keepalive(self):
        while not self.stopped:
            time.sleep(PING_INTERVAL)
            if not self.ready.is_set():
                continue
            try:
                with self.wlock:
                    wsproto.send_frame(self.sock, 0x9, b"")
            except (OSError, EOFError):
                pass

    def send(self, sid, kind, data=b""):
        sock = self.sock
        if sock is None:
            return
        try:
            with self.wlock:
                wsproto.send_frame(sock, 0x2, HDR.pack(sid, kind) + data)
        except (OSError, EOFError):
            pass

    def attach(self, remote, sink):
        if not self.ready.wait(15):
            sink.close()
            return
        entry = {"sink": sink, "outq": queue.Queue(QMAX), "local_fin": False,
                 "remote_fin": False, "lock": threading.Lock(), "closed": False, "w_done": False}
        with self.lock:
            self.next_id += 1
            sid = self.next_id
            self.streams[sid] = entry
        self.send(sid, OPEN, struct.pack("!H", remote))
        threading.Thread(target=self._pump, args=(sid, sink), daemon=True).start()
        threading.Thread(target=self._writer, args=(sid, entry), daemon=True).start()
        return sid

    def _writer(self, sid, entry):
        outq = entry["outq"]
        sink = entry["sink"]
        try:
            while True:
                item = outq.get()
                if item is ABORT:
                    return
                if item is SHUTDOWN:
                    sink.shutdown_wr()
                    return
                sink.feed(item)
        except OSError:
            self.send(sid, RESET)
            self._finish(sid, local=True, remote=True)
        finally:
            with entry["lock"]:
                entry["w_done"] = True
                ready = entry["closed"]
            if ready:
                sink.close()

    def _pump(self, sid, sink):
        try:
            while True:
                data = sink.recv()
                if not data:
                    break
                self.send(sid, DATA, data)
        except OSError:
            pass
        finally:
            self.send(sid, FIN)
            self._finish(sid, local=True)

    def _finish(self, sid, local=False, remote=False):
        with self.lock:
            entry = self.streams.get(sid)
            if not entry:
                return
        with entry["lock"]:
            entry["local_fin"] |= local
            entry["remote_fin"] |= remote
            done = entry["local_fin"] and entry["remote_fin"]
            ready = entry["w_done"]
            if done:
                entry["closed"] = True
        if done:
            with self.lock:
                self.streams.pop(sid, None)
            if ready:
                entry["sink"].close()

    def _reader(self, sock):
        try:
            while True:
                opcode, payload = wsproto.recv_frame(sock)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    with self.wlock:
                        wsproto.send_frame(sock, 0xA, payload)
                    continue
                if opcode != 0x2 or len(payload) < HDR.size:
                    continue
                sid, kind = HDR.unpack(payload[:HDR.size])
                body = payload[HDR.size:]
                if kind == DATA:
                    with self.lock:
                        entry = self.streams.get(sid)
                    if entry:
                        with entry["lock"]:
                            closed = entry["closed"]
                        if not closed:
                            try:
                                entry["outq"].put_nowait(body)
                            except queue.Full:
                                self.send(sid, RESET)
                                with self.lock:
                                    self.streams.pop(sid, None)
                                with entry["lock"]:
                                    entry["closed"] = True
                                entry["sink"].close()
                elif kind == FIN:
                    with self.lock:
                        entry = self.streams.get(sid)
                    if entry:
                        try:
                            entry["outq"].put_nowait(SHUTDOWN)
                        except queue.Full:
                            pass
                        self._finish(sid, remote=True)
                elif kind == RESET:
                    with self.lock:
                        entry = self.streams.pop(sid, None)
                    if entry:
                        with entry["lock"]:
                            entry["closed"] = True
                        try:
                            entry["outq"].put_nowait(ABORT)
                        except queue.Full:
                            pass
                        entry["sink"].close()
        except (OSError, EOFError):
            pass
        finally:
            wsproto.drain(sock)


def data_lane(mux, remote):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(DATA_TIMEOUT)
    port = listener.getsockname()[1]

    def run():
        try:
            conn, _ = listener.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(None)
            mux.attach(remote, Sink(conn))
        except OSError:
            pass
        finally:
            listener.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def rewrite(line, mux):
    if line.startswith(b"227"):
        match = PASV.search(line)
        if match:
            remote = int(match.group(5)) * 256 + int(match.group(6))
            local = data_lane(mux, remote)
            return line[:match.start()] + f"127,0,0,1,{local // 256},{local % 256}".encode() + line[match.end():]
    elif line.startswith(b"229"):
        match = EPSV.search(line)
        if match:
            remote = int(match.group(1))
            local = data_lane(mux, remote)
            return line[:match.start()] + f"(|||{local}|)".encode() + line[match.end():]
    return line


class Tunnel:
    """A local set of listeners that mirror remote ports over one WebSocket."""

    def __init__(self, url):
        host, port, path, tls = parse(url)
        self.mux = Mux(lambda: wsproto.connect(host, port, path, tls))
        self.listeners = []

    def wait_ready(self, timeout=15):
        return self.mux.ready.wait(timeout)

    def forward(self, remote, local=0):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", local))
        listener.listen(16)
        listener.settimeout(None)
        actual = listener.getsockname()[1]
        ftp = remote == 21

        def accept():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(None)
                if ftp:
                    sink = FtpSink(conn, self.mux)
                    sink.sid = self.mux.attach(21, sink)
                else:
                    self.mux.attach(remote, Sink(conn))

        self.listeners.append(listener)
        threading.Thread(target=accept, daemon=True).start()
        return actual

    def close(self):
        self.mux.stop()
        for listener in self.listeners:
            try:
                listener.close()
            except OSError:
                pass
