import base64
import hashlib
import os
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def xor_mask(payload, key):
    size = len(payload)
    if not size:
        return payload
    mask = (key * (size // 4 + 1))[:size]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(mask, "big")).to_bytes(size, "big")


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
    sock.sendall(header + key + xor_mask(payload, key))


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
        payload = xor_mask(recv_exact(sock, size), key) if size else b""
    else:
        payload = recv_exact(sock, size) if size else b""
    return opcode, payload


def keepalive(sock):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        idle = getattr(socket, "TCP_KEEPIDLE", getattr(socket, "TCP_KEEPALIVE", None))
        if idle is not None:
            sock.setsockopt(socket.IPPROTO_TCP, idle, 15)
        for name, value in (("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 3)):
            opt = getattr(socket, name, None)
            if opt is not None:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
    except OSError:
        pass


def connect(host, port, path, tls):
    sock = socket.create_connection((host, port), timeout=15)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    keepalive(sock)
    if tls:
        import ssl
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
    sock.settimeout(None)
    return sock


def drain(sock):
    try:
        sock.settimeout(2)
        sock.shutdown(socket.SHUT_WR)
    except (OSError, ValueError):
        pass
    try:
        while sock.recv(65536):
            pass
    except (OSError, ValueError):
        pass
    try:
        sock.close()
    except OSError:
        pass
