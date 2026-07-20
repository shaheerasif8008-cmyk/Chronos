import pytest


def test_egress_allowlist_normalizes_domains_and_rejects_internal_routes():
    from core.egress_policy import parse_egress_allowlist

    assert parse_egress_allowlist("GitHub.com, *.Example.COM,github.com") == [
        "github.com",
        "*.example.com",
    ]
    for unsafe in ("127.0.0.1", "10.0.0.0/8", "localhost", "https://example.com", "example.com:443"):
        with pytest.raises(ValueError):
            parse_egress_allowlist(unsafe)


def test_computer_consent_requires_exact_subset_of_operator_ceiling():
    from core.egress_policy import normalize_consent_domains

    policy = "github.com,*.client.example"
    assert normalize_consent_domains(
        ["github.com", "api.client.example"], policy=policy
    ) == ["github.com", "api.client.example"]
    with pytest.raises(PermissionError, match="not allowed"):
        normalize_consent_domains(["evil.example"], policy=policy)
    with pytest.raises(ValueError, match="exact domains"):
        normalize_consent_domains(["*.client.example"], policy=policy)
