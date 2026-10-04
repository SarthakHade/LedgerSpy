"""Tests run with outbound network blocked (pytest-socket, configured in pyproject.toml)."""

import socket
import urllib.request

import pytest
from pytest_socket import SocketConnectBlockedError

# pytest-socket warns on every blocked attempt; here blocking is the expected outcome.
pytestmark = pytest.mark.filterwarnings("ignore:A test tried to use socket:UserWarning")


def test_guard_is_active(pytestconfig):
    assert pytestconfig.getoption("allow_hosts") == "127.0.0.1"


@pytest.mark.parametrize("address", [("192.0.2.1", 80), ("8.8.8.8", 53), ("10.0.0.1", 443)])
def test_outbound_tcp_is_blocked(address):
    with pytest.raises(SocketConnectBlockedError):
        socket.create_connection(address, timeout=1)


def test_outbound_http_is_blocked():
    with pytest.raises(SocketConnectBlockedError):
        urllib.request.urlopen("http://192.0.2.1/", timeout=1)  # noqa: S310 — the point is that it fails


def test_outbound_udp_is_blocked():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(SocketConnectBlockedError):
            sock.connect(("192.0.2.1", 53))
    finally:
        sock.close()


def test_loopback_is_still_allowed():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        socket.create_connection(server.getsockname(), timeout=1).close()
    finally:
        server.close()
