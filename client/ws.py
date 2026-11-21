import base64
import hashlib
import os
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


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


def connect(host, port, path, tls):
    sock = socket.create_connection((host, port), timeout=15)
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
    return sock
