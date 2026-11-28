"""Browser terminal: xterm.js page plus WebSocket-to-PTY bridge."""

import fcntl
import json
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import termios
import threading

from . import wsproto

PATH = "/my-sandbox/xterm"
WS_PATH = PATH + "/ws"

PING_INTERVAL = 15

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mybox terminal</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css">
<style>
  html, body { height: 100%; margin: 0; background: #000; }
  #term { height: 100%; box-sizing: border-box; padding: 6px; }
</style>
</head>
<body>
<div id="term"></div>
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"></script>
<script>
(function () {
  function fitCtor() {
    if (window.FitAddon && window.FitAddon.FitAddon) return window.FitAddon.FitAddon;
    return window.FitAddon;
  }
  var term = new Terminal({ cursorBlink: true, fontFamily: 'Menlo, Consolas, monospace' });
  var fit = new (fitCtor())();
  term.loadAddon(fit);
  term.open(document.getElementById('term'));
  fit.fit();

  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  var ws = new WebSocket(proto + '//' + location.host + '__WS_PATH__' + location.search);
  ws.binaryType = 'arraybuffer';
  function resize() {
    if (ws.readyState === WebSocket.OPEN)
      ws.send(JSON.stringify({ cols: term.cols, rows: term.rows }));
  }
  ws.onopen = function () { resize(); term.focus(); };
  ws.onmessage = function (e) {
    if (typeof e.data === 'string') return;
    term.write(new Uint8Array(e.data));
  };
  ws.onclose = function () { term.write('\\r\\n[mybox] connection closed\\r\\n'); };
  ws.onerror = function () { term.write('\\r\\n[mybox] websocket error\\r\\n'); };
  term.onData(function (d) {
    if (ws.readyState === WebSocket.OPEN) ws.send(new TextEncoder().encode(d));
  });
  term.onResize(resize);
  window.addEventListener('resize', function () { fit.fit(); });
  term.focus();
})();
</script>
</body>
</html>
""".replace("__WS_PATH__", WS_PATH)


def _winsize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _shutdown(conn):
    try:
        conn.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def run(conn):
    """Serve one upgraded WebSocket: relay bytes between it and a private PTY shell."""
    master, slave = pty.openpty()
    _winsize(slave, 80, 24)
    env = dict(os.environ, TERM="xterm-256color")
    proc = subprocess.Popen(
        ["/bin/bash", "-l"],
        stdin=slave, stdout=slave, stderr=slave,
        env=env, close_fds=True, start_new_session=True,
    )
    os.close(slave)

    wlock = threading.Lock()
    stop = threading.Event()

    def send(opcode, payload=b""):
        try:
            with wlock:
                wsproto.send_frame(conn, opcode, payload, mask=False)
        except (OSError, EOFError):
            stop.set()
            _shutdown(conn)

    def pump():
        while not stop.is_set():
            try:
                ready, _, _ = select.select([master], [], [], 0.5)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            send(0x2, data)
        stop.set()
        _shutdown(conn)

    def keepalive():
        while not stop.wait(PING_INTERVAL):
            send(0x9, b"")

    threading.Thread(target=pump, daemon=True).start()
    threading.Thread(target=keepalive, daemon=True).start()

    try:
        while not stop.is_set():
            opcode, payload = wsproto.recv_frame(conn)
            if opcode == 0x8:
                break
            if opcode == 0x9:
                send(0xA, payload)
            elif opcode in (0x1, 0x2):
                if opcode == 0x1:
                    try:
                        msg = json.loads(payload.decode())
                        cols = max(1, min(int(msg["cols"]), 500))
                        rows = max(1, min(int(msg["rows"]), 200))
                        _winsize(master, cols, rows)
                    except (ValueError, KeyError, TypeError, OSError):
                        pass
                elif payload:
                    try:
                        os.write(master, payload)
                    except OSError:
                        break
    except (OSError, EOFError, ValueError):
        pass
    finally:
        stop.set()
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.close(master)
        except OSError:
            pass
        wsproto.drain(conn)
