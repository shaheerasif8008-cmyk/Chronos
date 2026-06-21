"""W1 Phase 1 — tenant label extraction from a Host header."""
from __future__ import annotations

import pytest

from core.tenancy import RESERVED_LABELS, extract_tenant_label


@pytest.mark.parametrize(
    "host,expected",
    [
        ("novatech.cognisiatech.com", "novatech"),
        ("novatech.cognisiatech.com:443", "novatech"),
        ("acme.localhost", "acme"),
        ("acme.localhost:8000", "acme"),
        ("acme.lvh.me", "acme"),
        ("cognisiatech.com", None),
        ("www.cognisiatech.com", None),
        ("app.cognisiatech.com", None),
        ("api.cognisiatech.com", None),
        ("localhost", None),
        ("test", None),
        ("", None),
    ],
)
def test_extract_tenant_label(host, expected):
    assert extract_tenant_label(host, base_domain="cognisiatech.com") == expected


def test_reserved_labels_are_blocked():
    for label in ("app", "www", "api", "admin"):
        assert label in RESERVED_LABELS
