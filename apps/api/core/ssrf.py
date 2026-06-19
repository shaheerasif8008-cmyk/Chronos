"""SSRF guard for outbound, LLM/agent-influenced URL fetches.

Any tool that fetches a URL whose host can be influenced by model output or
untrusted content (browser fetch, generic HTTP connector, remote MCP) must run
the target through :func:`assert_safe_url` first. It blocks non-HTTP schemes and
resolves the host to reject loopback, private (RFC1918), link-local (including
the cloud metadata endpoint ``169.254.169.254``), and other reserved ranges.

This is a hostname→IP resolve-then-check; callers that follow redirects must
re-check each hop (httpx exposes the final URL, browsers should disable cross
-origin navigation or re-validate). It is a defence-in-depth boundary, not a
substitute for not sending credentials to untrusted hosts.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    """Raised when a URL targets a disallowed scheme or address."""


_ALLOWED_SCHEMES = {"http", "https"}


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local      # includes 169.254.0.0/16 (cloud metadata)
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_safe_url(url: str) -> str:
    """Validate ``url`` for outbound fetching, returning it unchanged if safe.

    Raises :class:`UnsafeURLError` for non-HTTP schemes or hosts that resolve to
    loopback/private/link-local/reserved addresses.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme not allowed: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    # A literal IP in the URL is checked directly; otherwise resolve every A/AAAA
    # record and block if ANY of them is internal (defeats DNS-rebinding-ish
    # tricks where one record is public and another is private).
    try:
        literal = ipaddress.ip_address(host)
        candidates = [literal]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or None, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise UnsafeURLError(f"could not resolve host: {host}") from exc
        candidates = []
        for info in infos:
            addr = info[4][0]
            try:
                candidates.append(ipaddress.ip_address(addr))
            except ValueError:
                continue

    if not candidates:
        raise UnsafeURLError(f"no resolvable address for host: {host}")
    for ip in candidates:
        if _ip_is_blocked(ip):
            raise UnsafeURLError(f"host resolves to a blocked address: {ip}")
    return url
