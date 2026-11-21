import os
import socket
import socketserver
import sys
import time

ROOT = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.expanduser("~")
USER = os.environ.get("FTP_USER", "root")
PASS = os.environ.get("FTP_PASS", "myboxpass")
DATA_TIMEOUT = 30


class FTP(socketserver.StreamRequestHandler):
    pasv = None
    cwd = ROOT
    user = None
    rename_from = None

    def reply(self, text):
        self.wfile.write((text + "\r\n").encode())
        self.wfile.flush()

    def handle(self):
        self.reply("220 my-sandbox ftpd ready")
        while True:
            line = self.rfile.readline()
            if not line:
                break
            head, _, arg = line.decode(errors="replace").strip().partition(" ")
            cmd = head.upper()
            method = getattr(self, "cmd_" + cmd.lower(), None)
            if method is None:
                self.reply("502 not implemented")
                continue
            if method(arg) is False:
                break
            if cmd == "QUIT":
                break
        self.close_pasv()

    def cmd_user(self, arg):
        self.user = arg
        self.reply("331 password required")

    def cmd_pass(self, arg):
        if arg == PASS:
            self.reply("230 logged in")
        else:
            self.reply("530 denied")

    def cmd_syst(self, arg):
        self.reply("215 UNIX Type: L8")

    def cmd_feat(self, arg):
        self.reply("211-Features\r\n PASV\r\n EPSV\r\n SIZE\r\n UTF8\r\n211 End")

    def cmd_type(self, arg):
        self.reply("200 type set")

    def cmd_noop(self, arg):
        self.reply("200 ok")

    def cmd_opts(self, arg):
        self.reply("200 ok")

    def cmd_clnt(self, arg):
        self.reply("200 ok")

    def cmd_rest(self, arg):
        self.reply("200 ok")

    def cmd_allo(self, arg):
        self.reply("200 ok")

    def cmd_site(self, arg):
        self.reply("200 ok")

    def cmd_mdtm(self, arg):
        parts = arg.split(" ", 1)
        if len(parts) == 2 and len(parts[0]) == 14 and parts[0].isdigit():
            ts, name = parts
            target = self.resolve(name)
            if not target or not os.path.isfile(target):
                self.reply("550 not found")
                return
            try:
                t = time.mktime(time.strptime(ts, "%Y%m%d%H%M%S"))
                os.utime(target, (t, t))
                self.reply("213 " + ts)
            except (OSError, ValueError):
                self.reply("550 failed")
        else:
            target = self.resolve(arg)
            if target and os.path.isfile(target):
                t = time.localtime(os.path.getmtime(target))
                self.reply("213 " + time.strftime("%Y%m%d%H%M%S", t))
            else:
                self.reply("550 not found")

    def cmd_rnfr(self, arg):
        target = self.resolve(arg)
        if target and os.path.exists(target):
            self.rename_from = target
            self.reply("350 ready for rnfr")
        else:
            self.reply("550 not found")

    def cmd_rnto(self, arg):
        if not self.rename_from:
            self.reply("503 bad sequence")
            return
        target = self.resolve(arg, create=True)
        if not target:
            self.reply("550 permission denied")
        else:
            try:
                os.rename(self.rename_from, target)
                self.reply("250 renamed")
            except OSError:
                self.reply("550 rename failed")
            self.rename_from = None

    def cmd_pwd(self, arg):
        self.reply(f'257 "{self.rel(self.cwd)}" is current directory')

    def cmd_cwd(self, arg):
        target = self.resolve(arg)
        if target and os.path.isdir(target):
            self.cwd = target
            self.reply("250 directory changed")
        else:
            self.reply("550 no such directory")

    def cmd_cdup(self, arg):
        target = self.resolve("..")
        if target:
            self.cwd = target
        self.reply("250 directory changed")

    def cmd_pasv(self, arg):
        sock = self.open_pasv()
        port = sock.getsockname()[1]
        self.reply(f"227 Entering Passive Mode (127,0,0,1,{port // 256},{port % 256})")

    def cmd_epsv(self, arg):
        sock = self.pasv or self.open_pasv()
        self.reply(f"229 Entering Extended Passive Mode (|||{sock.getsockname()[1]}|)")

    def cmd_size(self, arg):
        target = self.resolve(arg)
        if target and os.path.isfile(target):
            self.reply(f"213 {os.path.getsize(target)}")
        else:
            self.reply("550 not found")

    def cmd_list(self, arg):
        names = sorted(os.listdir(self.cwd))
        lines = "".join(self.listing(name) for name in names)
        self.transfer(lines.encode())

    def cmd_nlst(self, arg):
        names = sorted(os.listdir(self.cwd))
        self.transfer("".join(name + "\r\n" for name in names).encode())

    def cmd_retr(self, arg):
        target = self.resolve(arg)
        if not target or not os.path.isfile(target):
            self.reply("550 not found")
            return
        self.reply("150 opening data connection")
        conn = self.accept_data()
        if conn is None:
            return
        try:
            with open(target, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    conn.sendall(chunk)
        except OSError:
            pass
        finally:
            conn.close()
        self.reply("226 transfer complete")

    def cmd_stor(self, arg):
        target = self.resolve(arg, create=True)
        if not target:
            self.reply("550 permission denied")
            return
        self.reply("150 opening data connection")
        conn = self.accept_data()
        if conn is None:
            return
        total = 0
        try:
            with open(target, "wb") as f:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
        except OSError:
            pass
        finally:
            conn.close()
        self.reply("226 transfer complete")

    def cmd_dele(self, arg):
        target = self.resolve(arg)
        if target and os.path.isfile(target):
            os.remove(target)
            self.reply("250 deleted")
        else:
            self.reply("550 not found")

    def cmd_mkd(self, arg):
        target = self.resolve(arg, create=True)
        if target:
            os.makedirs(target, exist_ok=True)
            self.reply(f'257 "{self.rel(target)}" created')
        else:
            self.reply("550 permission denied")

    def cmd_quit(self, arg):
        self.reply("221 bye")

    def transfer(self, data):
        self.reply("150 opening data connection")
        conn = self.accept_data()
        if conn is None:
            return
        try:
            conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()
        self.reply("226 transfer complete")

    def open_pasv(self):
        self.close_pasv()
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.pasv = sock
        return sock

    def accept_data(self):
        if self.pasv is None:
            self.reply("425 use PASV or EPSV first")
            return None
        self.pasv.settimeout(DATA_TIMEOUT)
        port = self.pasv.getsockname()[1]
        try:
            conn, peer = self.pasv.accept()
        except OSError as exc:
            self.reply("425 data connection failed")
            conn = None
        self.close_pasv()
        if conn is not None:
            conn.settimeout(DATA_TIMEOUT)
        return conn

    def close_pasv(self):
        if self.pasv is not None:
            try:
                self.pasv.close()
            except OSError:
                pass
            self.pasv = None

    def resolve(self, arg, create=False):
        if not arg:
            return self.cwd
        target = os.path.abspath(os.path.join(self.cwd, arg))
        prefix = ROOT.rstrip(os.sep) + os.sep
        if target != ROOT and not target.startswith(prefix):
            return None
        if create and not os.path.isdir(os.path.dirname(target)):
            return None
        return target

    def rel(self, path):
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        return "/" if rel == "." else "/" + rel

    def listing(self, name):
        path = os.path.join(self.cwd, name)
        if os.path.isdir(path):
            return f"drwxr-xr-x 1 0 0 0 Jan 01 00:00 {name}\r\n"
        return f"-rw-r--r-- 1 0 0 {os.path.getsize(path)} Jan 01 00:00 {name}\r\n"


if __name__ == "__main__":
    port = int(os.environ.get("FTP_PORT", 21))
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), FTP) as server:
        server.serve_forever()
