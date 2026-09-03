import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safe_id import validate_host, validate_id


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "192.168.1.10", "8.8.8.8",
    "worker-1", "worker-1.internal.corp",
    "[::1]", "[2001:db8::1]",
])
def test_valid_hosts(host):
    assert validate_host(host) == host


@pytest.mark.parametrize("host", [
    "", "  ", "http://evil.com", "evil.com:7000", "user@evil.com",
    "../etc/passwd", "..", ".", "1.2.3.4/24", "1.2.3.4/",
    "0.0.0.0", "255.255.255.255", "224.0.0.1",
    "::1", "fe80::1", "[not-an-ip]", "[::1",
    "my_host", "-leading-.hyphen", "a b", "exa mple.com", "example.com.",
    "x" * 254,
])
def test_invalid_hosts(host):
    with pytest.raises(ValueError):
        validate_host(host)


def test_private_host_blocked_by_env(monkeypatch):
    monkeypatch.setenv("ORCH_BLOCK_PRIVATE_HOSTS", "1")
    for host in ["127.0.0.1", "192.168.1.5", "10.0.0.8", "[::1]", "[fe80::1]"]:
        with pytest.raises(ValueError):
            validate_host(host)
    assert validate_host("8.8.8.8") == "8.8.8.8"


def test_private_host_allowed_by_default():
    assert validate_host("192.168.1.5") == "192.168.1.5"


def test_validate_id_rejects_traversal():
    with pytest.raises(ValueError):
        validate_id("../evil")
    assert validate_id("task-Abc_123") == "task-Abc_123"
