from __future__ import annotations
import socket
import pytest

from security_utils import NetworkFetchError, URLValidationError, safe_get, validate_public_url


def resolver_for(ip):
    def _resolver(host, port, type=socket.SOCK_STREAM):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]
    return _resolver

@pytest.mark.parametrize("url,ip", [
    ("https://example.com/x", "93.184.216.34"),
    ("http://8.8.8.8/", "8.8.8.8"),
])
def test_public_url_allowed(url, ip):
    assert validate_public_url(url, resolver=resolver_for(ip)) == url

@pytest.mark.parametrize("url,ip", [
    ("http://localhost/", "127.0.0.1"),
    ("http://127.0.0.1/", "127.0.0.1"),
    ("http://192.168.1.10/", "192.168.1.10"),
    ("http://10.1.2.3/", "10.1.2.3"),
    ("http://172.16.0.1/", "172.16.0.1"),
    ("http://172.31.255.254/", "172.31.255.254"),
    ("http://[::1]/", "::1"),
    ("http://169.254.1.1/", "169.254.1.1"),
    ("http://169.254.169.254/latest/meta-data", "169.254.169.254"),
    ("http://100.64.0.1/", "100.64.0.1"),
])
def test_internal_urls_blocked(url, ip):
    with pytest.raises(URLValidationError):
        validate_public_url(url, resolver=resolver_for(ip))

@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/a", "not a url", "http:///missing-host"])
def test_bad_or_unsupported_scheme(url):
    with pytest.raises(URLValidationError):
        validate_public_url(url, resolver=resolver_for("93.184.216.34"))


class FakeResponse:
    def __init__(self, status=200, headers=None, content=b"ok", url="https://example.com/"):
        self.status_code = status
        self.headers = headers or {}
        self._data = content
        self.url = url
        self.encoding = "utf-8"
    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i+chunk_size]
    def close(self):
        pass

class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)


def test_redirect_to_private_is_blocked_before_second_request():
    session = FakeSession([
        FakeResponse(302, {"Location": "http://10.0.0.1/admin"}, b"", "https://example.com/start")
    ])
    with pytest.raises(URLValidationError):
        safe_get("https://example.com/start", session=session, resolver=resolver_for("93.184.216.34"))
    assert session.calls == ["https://example.com/start"]


def test_safe_get_public_redirect():
    session = FakeSession([
        FakeResponse(302, {"Location": "/next"}, b"", "https://example.com/start"),
        FakeResponse(200, {}, b"hello", "https://example.com/next"),
    ])
    response = safe_get("https://example.com/start", session=session, resolver=resolver_for("93.184.216.34"))
    assert response.content == b"hello"
    assert len(session.calls) == 2


def test_response_size_limit():
    session = FakeSession([FakeResponse(200, {}, b"x" * 100, "https://example.com/")])
    with pytest.raises(NetworkFetchError):
        safe_get("https://example.com/", session=session, resolver=resolver_for("93.184.216.34"), max_bytes=50)
