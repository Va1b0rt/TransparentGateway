import importlib.util
import socket
import struct
from pathlib import Path

MODULE = Path(__file__).parents[1] / "gateway" / "udp2tcp_dns.py"
spec = importlib.util.spec_from_file_location("udp2tcp_dns", MODULE)
relay = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(relay)


def test_extracts_destination_from_linux_original_destination_cmsg():
    sockaddr = struct.pack("=H", socket.AF_INET) + struct.pack("!H", 53) + socket.inet_aton("203.0.113.53") + b"\0" * 8
    assert relay.original_destination([(socket.SOL_IP, relay.IP_ORIGDSTADDR, sockaddr)]) == ("203.0.113.53", 53)


def test_extracts_destination_from_linux_pktinfo_cmsg():
    pktinfo = struct.pack(
        "=I4s4s",
        0,
        socket.inet_aton("192.0.2.1"),
        socket.inet_aton("203.0.113.53"),
    )
    assert relay.original_destination(
        [(socket.SOL_IP, relay.IP_PKTINFO, pktinfo)]
    ) == ("203.0.113.53", 53)


def test_rejects_packet_without_original_destination():
    try:
        relay.original_destination([])
    except ValueError as error:
        assert "original destination" in str(error)
    else:
        raise AssertionError("missing cmsg must be rejected")
