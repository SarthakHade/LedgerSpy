"""Network guard for the P21 offline check (never shipped in the bundle).

Placed on PYTHONPATH, Python imports it at start-up in every process of the checked installation (pip
during install, the server, the PDF / OCR worker it spawns). Any attempt to resolve a non-loopback name or
connect to a non-loopback address is refused (OSError) and appended to the file named by LEDGERSPY_NETLOG,
so the check can prove that nothing tried to reach the network. Loopback (127.0.0.0/8, ::1, localhost) is
allowed.
"""

import os
import socket
import traceback

_LOG = os.environ.get("LEDGERSPY_NETLOG")
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "", None}


def _loopback(host) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    h = str(host)
    return h.startswith("127.") or h in ("::1", "0:0:0:0:0:0:0:1")


def _record(kind: str, target) -> None:
    if _LOG:
        stack = "".join(traceback.format_stack(limit=6)[:-2]).replace("\n", " | ")
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\t{kind}\t{target!r}\t{stack}\n")


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(host):
        _record("connect", address)
        raise OSError(f"network disabled by the offline check: connect to {address!r}")
    return _connect(self, address)


def _guarded_connect_ex(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(host):
        _record("connect_ex", address)
        return 10051  # WSAENETUNREACH
    return _connect_ex(self, address)


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _loopback(host):
        _record("getaddrinfo", host)
        raise socket.gaierror(f"network disabled by the offline check: resolve {host!r}")
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
socket.getaddrinfo = _guarded_getaddrinfo
