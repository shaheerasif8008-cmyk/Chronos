from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from connectors.github_repo_publisher import (
    GitHubPublicationError,
    GitHubRepoPublisher,
)


BASE_SHA = "b" * 40
COMMIT_SHA = "c" * 40
TOKEN = "github-oauth-token-must-never-be-persisted"
MARKER = "chronos-request:1234567890abcdef12345678"


class FakeGitHub:
    def __init__(self, *, scopes: str = "repo, workflow, read:user") -> None:
        self.scopes = scopes
        self.base_sha = BASE_SHA
        self.head_sha: str | None = None
        self.pull: dict[str, Any] | None = None
        self.graphql_calls = 0
        self.pull_create_calls = 0
        self.requests: list[tuple[str, str, Any]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        payload = json.loads(request.content) if request.content else None
        path = request.url.raw_path.decode().split("?", 1)[0]
        self.requests.append((request.method, path, payload))
        headers = {"x-oauth-scopes": self.scopes}
        if request.method == "GET" and path == "/repos/acme/widget":
            return httpx.Response(
                200,
                json={"private": True, "permissions": {"push": True}},
                headers=headers,
            )
        if request.method == "GET" and path == "/repos/acme/widget/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": self.base_sha}})
        if request.method == "GET" and path == "/repos/acme/widget/git/ref/heads/feature%2Fsafe":
            if self.head_sha is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.head_sha}})
        if request.method == "POST" and path == "/repos/acme/widget/git/refs":
            assert payload == {"ref": "refs/heads/feature/safe", "sha": BASE_SHA}
            self.head_sha = BASE_SHA
            return httpx.Response(201, json={"object": {"sha": BASE_SHA}})
        if request.method == "POST" and path == "/graphql":
            self.graphql_calls += 1
            input_payload = payload["variables"]["input"]
            assert input_payload["expectedHeadOid"] == BASE_SHA
            assert input_payload["clientMutationId"] == "request-key"
            assert input_payload["fileChanges"]["additions"] == [
                {"path": "app.py", "contents": "cHJpbnQoJ29rJykK"}
            ]
            assert MARKER in input_payload["message"]["body"]
            self.head_sha = COMMIT_SHA
            return httpx.Response(
                200,
                json={
                    "data": {
                        "createCommitOnBranch": {
                            "commit": {
                                "oid": COMMIT_SHA,
                                "url": f"https://github.com/acme/widget/commit/{COMMIT_SHA}",
                            },
                            "ref": {"name": "feature/safe"},
                        }
                    }
                },
            )
        if request.method == "GET" and path == f"/repos/acme/widget/git/commits/{COMMIT_SHA}":
            return httpx.Response(200, json={"message": f"Approved\n\n[{MARKER}]"})
        if request.method == "GET" and path == "/repos/acme/widget/pulls":
            return httpx.Response(200, json=[self.pull] if self.pull else [])
        if request.method == "POST" and path == "/repos/acme/widget/pulls":
            self.pull_create_calls += 1
            assert MARKER in payload["body"]
            self.pull = {
                "id": 7001,
                "node_id": "PR_node_7",
                "number": 7,
                "html_url": "https://github.com/acme/widget/pull/7",
                "body": payload["body"],
            }
            return httpx.Response(201, json=self.pull)
        raise AssertionError(f"Unexpected GitHub request: {request.method} {request.url}")


def _publisher(fake: FakeGitHub) -> GitHubRepoPublisher:
    return GitHubRepoPublisher(TOKEN, transport=fake.transport())


async def _publish(
    fake: FakeGitHub,
    state: dict[str, Any],
    persist: Callable[[dict[str, Any]], Any],
):
    return await _publisher(fake).publish(
        owner="acme",
        repo="widget",
        base="main",
        head="feature/safe",
        source_sha=BASE_SHA,
        title="Fix production bug",
        body="Verified test evidence.",
        additions=[{"path": "app.py", "contents": "cHJpbnQoJ29rJykK"}],
        deletions=[],
        marker=MARKER,
        key_hash="request-key",
        state=state,
        persist=persist,
    )


@pytest.mark.asyncio
async def test_github_publisher_creates_branch_signed_commit_and_real_pr_without_persisting_token() -> None:
    fake = FakeGitHub()
    states: list[dict[str, Any]] = []

    async def persist(state):
        states.append(dict(state))

    provider, final_state, newly_published = await _publish(
        fake, {"stage": "claimed"}, persist
    )

    assert newly_published is True
    assert provider == {
        "id": 7001,
        "node_id": "PR_node_7",
        "number": 7,
        "commit_oid": COMMIT_SHA,
        "url": "https://github.com/acme/widget/pull/7",
    }
    assert final_state["stage"] == "pr_created"
    assert [state["stage"] for state in states] == [
        "branch_created",
        "commit_created",
        "pr_created",
    ]
    assert fake.graphql_calls == 1
    assert fake.pull_create_calls == 1
    assert TOKEN not in json.dumps(states)
    assert TOKEN not in json.dumps(provider)
    assert TOKEN not in json.dumps([item[2] for item in fake.requests])


@pytest.mark.asyncio
async def test_crash_after_commit_is_recovered_without_duplicate_commit_or_pr() -> None:
    fake = FakeGitHub()
    durable_state = {"stage": "claimed"}
    crashed = False

    async def crash_after_commit(state):
        nonlocal durable_state, crashed
        if state["stage"] == "commit_created" and not crashed:
            crashed = True
            raise RuntimeError("simulated process crash")
        durable_state = dict(state)

    with pytest.raises(RuntimeError, match="simulated process crash"):
        await _publish(fake, durable_state, crash_after_commit)
    assert fake.graphql_calls == 1
    assert durable_state["stage"] == "branch_created"
    assert fake.head_sha == COMMIT_SHA

    async def persist(state):
        nonlocal durable_state
        durable_state = dict(state)

    provider, state, newly_published = await _publish(fake, durable_state, persist)
    assert provider["number"] == 7
    assert state["stage"] == "pr_created"
    assert newly_published is True
    assert fake.graphql_calls == 1
    assert fake.pull_create_calls == 1


@pytest.mark.asyncio
async def test_crash_after_pr_is_recovered_by_marker_without_duplicate_provider_write() -> None:
    fake = FakeGitHub()
    durable_state = {"stage": "claimed"}
    crashed = False

    async def crash_after_pr(state):
        nonlocal durable_state, crashed
        if state["stage"] == "pr_created" and not crashed:
            crashed = True
            raise RuntimeError("simulated post-provider crash")
        durable_state = dict(state)

    with pytest.raises(RuntimeError, match="simulated post-provider crash"):
        await _publish(fake, durable_state, crash_after_pr)
    assert durable_state["stage"] == "commit_created"
    assert fake.pull_create_calls == 1
    fake.base_sha = "e" * 40  # Base can advance after the already-created PR.

    async def persist(state):
        nonlocal durable_state
        durable_state = dict(state)

    provider, state, newly_published = await _publish(fake, durable_state, persist)
    assert provider["number"] == 7
    assert state["stage"] == "pr_created"
    assert newly_published is False
    assert fake.graphql_calls == 1
    assert fake.pull_create_calls == 1


@pytest.mark.asyncio
async def test_publisher_fails_truthfully_when_oauth_scope_cannot_push() -> None:
    fake = FakeGitHub(scopes="read:user")

    async def persist(_state):
        raise AssertionError("scope failure must occur before durable provider state")

    with pytest.raises(GitHubPublicationError) as exc_info:
        await _publish(fake, {"stage": "claimed"}, persist)
    assert exc_info.value.code == "github_repo_scope_required"
    assert fake.graphql_calls == 0
    assert fake.pull_create_calls == 0


@pytest.mark.asyncio
async def test_workflow_change_requires_explicit_workflow_oauth_scope() -> None:
    fake = FakeGitHub(scopes="repo, read:user")

    async def persist(_state):
        raise AssertionError("scope failure must occur before provider state")

    with pytest.raises(GitHubPublicationError) as exc_info:
        await _publisher(fake).publish(
            owner="acme",
            repo="widget",
            base="main",
            head="feature/safe",
            source_sha=BASE_SHA,
            title="Update workflow",
            body="Approved workflow change.",
            additions=[
                {
                    "path": ".github/workflows/ci.yml",
                    "contents": "bmFtZTogQ0kK",
                }
            ],
            deletions=[],
            marker=MARKER,
            key_hash="request-key",
            state={"stage": "claimed"},
            persist=persist,
        )
    assert exc_info.value.code == "github_workflow_scope_required"
    assert fake.graphql_calls == 0


def test_repo_pr_is_broker_hard_floor_and_github_oauth_requests_write_scopes() -> None:
    from connectors.oauth_apps import APPS
    from core.tool_broker import _ALWAYS_APPROVAL_TOOLS
    from runtime.tool_registry import ALWAYS_APPROVAL_TOOL_NAMES, REPO_CREATE_PR

    assert "repo.create_pr" in _ALWAYS_APPROVAL_TOOLS
    assert "repo__create_pr" in ALWAYS_APPROVAL_TOOL_NAMES
    properties = REPO_CREATE_PR["function"]["parameters"]["properties"]
    assert "approval_id" not in properties
    assert {"repo", "workflow"} <= set(APPS["github"].scopes)


@pytest.mark.asyncio
async def test_github_oauth_start_uses_direct_member_vault_even_when_composio_is_enabled(
    monkeypatch,
) -> None:
    from connectors import composio_client, oauth_apps
    from core.models import Member
    from routers import connectors as connector_router

    monkeypatch.setattr(composio_client, "is_configured", lambda: True)
    monkeypatch.setattr(composio_client, "is_composio_provider", lambda _provider: True)
    monkeypatch.setattr(
        oauth_apps,
        "get_client_credentials",
        lambda _app: ("github-client", "github-secret"),
    )

    async def forbidden_composio(*_args, **_kwargs):
        raise AssertionError("GitHub repo OAuth must not be redirected to Composio")

    async def allowed(*_args, **_kwargs):
        return True

    async def no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(connector_router, "_composio_oauth_start", forbidden_composio)
    monkeypatch.setattr(connector_router.permissions, "check", allowed)
    monkeypatch.setattr(connector_router.audit, "log", no_audit)

    result = await connector_router.generic_oauth_start(
        "github",
        Member(
            id="member-a",
            organization_id="org-a",
            email="member@example.com",
        ),
    )

    assert result["url"].startswith("https://github.com/login/oauth/authorize?")
    assert "scope=repo+workflow+read%3Auser+read%3Aorg" in result["url"]
    assert "client_id=github-client" in result["url"]
