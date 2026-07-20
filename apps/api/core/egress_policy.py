"""Validation helpers for provider-enforced outbound network allowlists."""
from __future__ import annotations

import ipaddress
import re

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def parse_egress_allowlist(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """Return a normalized, de-duplicated list of DNS allowlist entries.

    Organization policy may use an exact domain or a leading ``*.`` suffix
    rule. IP addresses, URLs, ports, localhost and private network literals are
    deliberately refused: client computer consent is expressed in readable DNS
    names and cannot accidentally open an internal network route.
    """

    if isinstance(value, str):
        items = value.split(",")
    else:
        items = list(value or [])
    normalized: list[str] = []
    for item in items:
        entry = str(item or "").strip().lower().rstrip(".")
        if not entry:
            continue
        wildcard = entry.startswith("*.")
        host = entry[2:] if wildcard else entry
        if any(char in host for char in "/:@?#"):
            raise ValueError(f"Invalid egress allowlist domain: {entry}")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("Egress allowlists must use DNS domains, not IP addresses")
        labels = host.split(".")
        if host == "localhost" or len(labels) < 2 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
            raise ValueError(f"Invalid egress allowlist domain: {entry}")
        canonical = f"*.{host}" if wildcard else host
        if canonical not in normalized:
            normalized.append(canonical)
    if len(normalized) > 100:
        raise ValueError("Egress allowlists may contain at most 100 domains")
    return normalized


def normalize_consent_domains(value: object, *, policy: str | list[str]) -> list[str]:
    """Validate exact user-approved domains against the organization ceiling."""

    if not isinstance(value, list) or not value:
        raise ValueError("Network capability requires at least one allowed egress domain")
    domains = parse_egress_allowlist([str(item) for item in value])
    if len(domains) > 20:
        raise ValueError("Computer consent may allow at most 20 egress domains")
    if any(domain.startswith("*.") for domain in domains):
        raise ValueError("Computer consent must use exact domains, not wildcard domains")
    ceiling = parse_egress_allowlist(policy)
    if not ceiling:
        raise PermissionError("Cloud computer egress allowlist is not configured")
    disallowed = [domain for domain in domains if not domain_allowed(domain, ceiling)]
    if disallowed:
        raise PermissionError(
            "Cloud computer egress domain is not allowed by organization policy: "
            + ", ".join(disallowed)
        )
    return domains


def domain_allowed(domain: str, policy: list[str]) -> bool:
    return any(
        domain == rule
        or (rule.startswith("*.") and domain.endswith(rule[1:]) and domain != rule[2:])
        for rule in policy
    )
