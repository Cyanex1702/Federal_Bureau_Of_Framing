from __future__ import annotations

import ipaddress
import socket
import threading
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter


class URLValidationError(ValueError):
    pass


class NetworkFetchError(RuntimeError):
    pass


_BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata",
    "instance-data",
}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home")
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("198.18.0.0/15"),
)
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}
_thread_local = threading.local()


def _is_blocked_ip(ip: ipaddress._BaseAddress) -> bool:
    if ip in _METADATA_IPS:
        return True
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    if getattr(ip, "is_site_local", False):
        return True
    return any(ip in network for network in _BLOCKED_NETWORKS)


def resolve_host_ips(
    hostname: str,
    port: int,
    resolver: Callable = socket.getaddrinfo,
) -> list[ipaddress._BaseAddress]:
    try:
        records = resolver(hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError) as exc:
        raise URLValidationError("The scanner could not resolve that hostname.") from exc
    ips: list[ipaddress._BaseAddress] = []
    for record in records:
        try:
            raw = record[4][0]
            ip = ipaddress.ip_address(raw.split("%", 1)[0])
        except Exception:
            continue
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise URLValidationError("The scanner could not resolve that hostname.")
    return ips


def validate_public_url(
    url: object,
    *,
    resolver: Callable = socket.getaddrinfo,
) -> str:
    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
    except Exception as exc:
        raise URLValidationError("Please enter a valid public http:// or https:// URL.") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise URLValidationError("Only public http:// and https:// URLs are supported.")
    if not parsed.hostname:
        raise URLValidationError("Please enter a valid public http:// or https:// URL.")
    if parsed.username or parsed.password:
        raise URLValidationError("URLs containing embedded credentials are not supported.")

    host = parsed.hostname.rstrip(".").casefold()
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise URLValidationError("That address is not available to the webpage scanner.")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
        ips = [literal]
    except ValueError:
        ips = resolve_host_ips(host, port, resolver=resolver)

    if any(_is_blocked_ip(ip) for ip in ips):
        raise URLValidationError("That address is not available to the webpage scanner.")
    return raw


def get_thread_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        # Avoid environment-controlled proxy routing for user-supplied scanner URLs.
        session.trust_env = False
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _thread_local.session = session
    return session


@dataclass
class SafeResponse:
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str
    encoding: str | None = None

    @property
    def text(self) -> str:
        encoding = self.encoding or "utf-8"
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise NetworkFetchError("The remote server returned an error response.")


def safe_get(
    url: object,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: tuple[float, float] | float = (6.0, 20.0),
    max_redirects: int = 5,
    max_bytes: int = 8 * 1024 * 1024,
    session=None,
    resolver: Callable = socket.getaddrinfo,
) -> SafeResponse:
    """SSRF-resistant bounded GET with manual redirect validation and size limits."""
    current = validate_public_url(url, resolver=resolver)
    client = session or get_thread_session()

    for redirect_index in range(max_redirects + 1):
        # Re-resolve immediately before every connection/redirect hop.
        validate_public_url(current, resolver=resolver)
        try:
            response = client.get(
                current,
                headers=dict(headers or {}),
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise NetworkFetchError("The remote page could not be reached.") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        response_headers = dict(getattr(response, "headers", {}) or {})

        if status in {301, 302, 303, 307, 308}:
            location = response_headers.get("Location") or response_headers.get("location")
            try:
                response.close()
            except Exception:
                pass
            if not location:
                raise NetworkFetchError("The remote server returned an invalid redirect.")
            if redirect_index >= max_redirects:
                raise NetworkFetchError("The remote page redirected too many times.")
            current = urljoin(current, location)
            validate_public_url(current, resolver=resolver)
            continue

        content_length = response_headers.get("Content-Length") or response_headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise NetworkFetchError("The remote response is larger than the scanner limit.")
            except ValueError:
                pass

        data = bytearray()
        try:
            iterator = response.iter_content(chunk_size=64 * 1024)
            for chunk in iterator:
                if not chunk:
                    continue
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise NetworkFetchError("The remote response is larger than the scanner limit.")
        finally:
            try:
                response.close()
            except Exception:
                pass

        final_url = str(getattr(response, "url", None) or current)
        # A custom transport/proxy must not be able to report an unchecked redirect target.
        validate_public_url(final_url, resolver=resolver)
        encoding = getattr(response, "encoding", None)
        return SafeResponse(status, response_headers, bytes(data), final_url, encoding)

    raise NetworkFetchError("The remote page redirected too many times.")
