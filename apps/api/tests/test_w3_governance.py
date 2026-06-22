import pytest


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int | str] = {}
        self.expiries: dict[str, int] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value, ex: int | None = None):
        self.values[key] = value
        if ex is not None:
            self.expiries[key] = ex

    async def incrby(self, key: str, amount: int):
        self.values[key] = int(self.values.get(key, 0)) + int(amount)
        return self.values[key]

    async def incr(self, key: str):
        return await self.incrby(key, 1)

    async def expire(self, key: str, seconds: int):
        self.expiries[key] = seconds


@pytest.mark.asyncio
async def test_model_usage_records_dollar_cost_and_hard_stops_when_budget_is_exhausted(monkeypatch):
    from core import governance

    redis = _FakeRedis()
    monkeypatch.setattr(governance, "redis_client", redis)

    async def fake_config(org_id: str):
        assert org_id == "org-cost"
        return governance.GovernanceConfig(
            daily_token_limit=0,
            daily_cost_limit_usd=0.000001,
            request_rate_per_minute=60,
            connector_rate_per_minute=60,
            max_task_queue_size=100,
            max_concurrent_runtimes=3,
        )

    monkeypatch.setattr(governance, "governance_config", fake_config)

    summary = await governance.record_model_usage(
        "org-cost",
        model="openrouter/openai/gpt-5.4-mini",
        total_tokens=1_000,
        prompt_tokens=500,
    )

    assert summary["cost_today_usd"] > 0
    assert summary["budget_hard_stop"] is True
    assert summary["suspended"] is True

    with pytest.raises(governance.GovernanceLimitExceeded, match="cost budget"):
        await governance.enforce_model_budget(
            "org-cost",
            model="openrouter/openai/gpt-5.4-mini",
            estimated_tokens=1,
        )


@pytest.mark.asyncio
async def test_task_creation_checks_governance_before_inserting(monkeypatch):
    from core.models import Member
    from core.governance import GovernanceLimitExceeded
    from routers import tasks

    async def fake_permission(*args, **kwargs):
        return True

    async def blocked(org_id: str):
        assert org_id == "org-blocked"
        raise GovernanceLimitExceeded("organization suspended")

    async def db_should_not_be_reached(_name: str):
        raise AssertionError("task insert must not happen after governance blocks admission")

    monkeypatch.setattr(tasks.permissions, "check", fake_permission)
    monkeypatch.setattr(tasks, "enforce_task_admission", blocked, raising=False)
    monkeypatch.setattr(tasks, "reflect_table", db_should_not_be_reached)

    with pytest.raises(GovernanceLimitExceeded, match="suspended"):
        await tasks.create_task_record(
            goal="do work",
            member=Member(id="member-1", organization_id="org-blocked", email="owner@example.com", role="owner"),
            triggered_by="manual",
        )


@pytest.mark.asyncio
async def test_vault_get_requires_matching_org_before_decrypting(monkeypatch):
    from connectors import vault
    from core.exceptions import VaultError

    from sqlalchemy import column, table

    _table = table(
        "vault_entries",
        column("organization_id"),
        column("vault_ref"),
        column("encrypted_data"),
    )

    class _Scalar:
        def scalar_one_or_none(self):
            return None

    class _Conn:
        async def execute(self, _stmt):
            return _Scalar()

    class _Begin:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *args):
            return False

    class _Engine:
        def begin(self):
            return _Begin()

    redis = _FakeRedis()
    monkeypatch.setattr(vault, "redis_client", redis)
    monkeypatch.setattr(vault, "engine", _Engine())
    async def fake_reflect_table(_name: str):
        return _table

    monkeypatch.setattr(vault, "reflect_table", fake_reflect_table)

    def decrypt_should_not_run(_encrypted: str):
        raise AssertionError("cross-tenant vault lookup must fail before decrypting")

    monkeypatch.setattr(vault, "_decrypt", decrypt_should_not_run)

    with pytest.raises(VaultError, match="not found"):
        await vault.get("vlt_from_other_org", org_id="org-b")
