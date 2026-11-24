import queue
import socket
import struct
import threading
import time
from urllib.parse import urlparse

from . import wsproto

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

        def accept():
            while True:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                conn.settimeout(None)
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
