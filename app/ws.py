"""Minimal WebSocket endpoint (stdlib only) for the admin connectivity test.

The app is a plain Flask/WSGI app served behind a reverse proxy, and the only
WebSocket need is a diagnostic ("is a WS connection usable through the proxy?").
Rather than pull in Flask-SocketIO (which changes the serving model), this
module hand-rolls the RFC 6455 handshake plus a small echo loop on the raw TCP
socket Werkzeug exposes. It is intentionally tiny: echo text/binary frames,
answer pings, and close cleanly.
"""

import base64
import hashlib
import struct
from typing import Any, Tuple

#: RFC 6455 magic GUID used to derive Sec-WebSocket-Accept.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def compute_accept(key: str) -> str:
    """Derive the Sec-WebSocket-Accept value for a client's handshake key."""
    digest = hashlib.sha1((key + _WS_GUID).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def _get_socket(environ: dict[str, Any]) -> Any:
    """Return the underlying TCP socket from the WSGI environ.

    Werkzeug's dev server (what this app runs under) exposes it as
    ``werkzeug.socket``; a couple of alternate names are tried for robustness.
    """
    for name in ("werkzeug.socket", "gunicorn.socket", "socket"):
        sock = environ.get(name)
        if sock is not None:
            return sock
    raise ConnectionError("No underlying socket available for a WebSocket upgrade.")


def _recv_exact(sock: Any, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Client closed the connection.")
        buf += chunk
    return buf


def _read_frame(sock: Any) -> Tuple[bool, int, bytes]:
    b0, b1 = _recv_exact(sock, 2)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        (length,) = struct.unpack(">H", _recv_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack(">Q", _recv_exact(sock, 8))
    mask = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bool(b0 & 0x80), opcode, payload


def _send_frame(sock: Any, payload: bytes, opcode: int = 0x1) -> None:
    header = bytes([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack(">H", length)
    else:
        header += bytes([127]) + struct.pack(">Q", length)
    sock.sendall(header + payload)


def run_echo(environ: dict[str, Any], key: str) -> None:
    """Perform the WS handshake on ``environ``'s socket, then echo until close.

    Blocks for the lifetime of the connection. Raises ConnectionError when the
    underlying socket is missing or the client disconnects.
    """
    sock = _get_socket(environ)
    sock.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {compute_accept(key)}\r\n"
            "\r\n"
        ).encode("latin-1")
    )
    try:
        while True:
            try:
                _fin, opcode, payload = _read_frame(sock)
            except (ConnectionError, OSError):
                break
            if opcode == 0x8:  # close
                try:
                    _send_frame(sock, payload, 0x8)
                except OSError:
                    pass
                break
            if opcode == 0x9:  # ping -> pong
                _send_frame(sock, payload, 0xA)
            elif opcode in (0x1, 0x2):  # text / binary -> echo
                _send_frame(sock, payload, opcode)
            # 0x0 (continuation) and reserved opcodes are ignored for a diagnostic.
    finally:
        try:
            sock.close()
        except OSError:
            pass
