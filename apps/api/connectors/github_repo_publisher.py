"""Approval-bound GitHub branch/PR publication without sandbox credentials."""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx


GITHUB_API_VERSION = "2026-03-10"
GITHUB_API_BASE = "https://api.github.com"


class GitHubPublicationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class GitHubRepoPublisher:
    """Create one branch commit and PR with crash-safe provider discovery.

    The OAuth token remains only in this short-lived API-side object. Provider
    errors are replaced without exception chaining because httpx request
    objects retain Authorization headers.
    """

    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._transport = transport

    def clear_credentials(self) -> None:
        """Erase the bearer token before control or exceptions leave the caller."""

        self._token = ""

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        try:
            async with httpx.AsyncClient(
                base_url=GITHUB_API_BASE,
                timeout=httpx.Timeout(30.0),
                follow_redirects=False,
                transport=self._transport,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._token}",
                    "X-GitHub-Api-Version": GITHUB_API_VERSION,
                    "User-Agent": "Chronos-Repo-Publisher",
                },
            ) as client:
                response = await client.request(method, path, json=payload, params=params)
            try:
                data = response.json()
            except (ValueError, TypeError):
                data = None
            return response.status_code, data, {
                key.lower(): value for key, value in response.headers.items()
            }
        except (httpx.HTTPError, OSError):
            raise GitHubPublicationError("github_provider_unavailable") from None

    async def publish(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any], bool]:
        try:
            return await self._publish(**kwargs)
        finally:
            self.clear_credentials()

    async def _publish(
        self,
        *,
        owner: str,
        repo: str,
        base: str,
        head: str,
        source_sha: str,
        title: str,
        body: str,
        additions: list[dict[str, Any]],
        deletions: list[dict[str, Any]],
        marker: str,
        key_hash: str,
        state: dict[str, Any],
        persist: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        repo_path = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        status, repo_info, headers = await self._request("GET", repo_path)
        if status in {401, 403}:
            raise GitHubPublicationError("github_push_not_authorized")
        if status != 200 or not isinstance(repo_info, dict):
            raise GitHubPublicationError("github_provider_unavailable")
        scopes = {
            scope.strip()
            for scope in str(headers.get("x-oauth-scopes") or "").split(",")
            if scope.strip()
        }
        if not scopes:
            raise GitHubPublicationError("github_scope_unverifiable")
        if bool(repo_info.get("private")):
            if "repo" not in scopes:
                raise GitHubPublicationError("github_repo_scope_required")
        elif not ({"repo", "public_repo"} & scopes):
            raise GitHubPublicationError("github_public_repo_scope_required")
        permissions = repo_info.get("permissions")
        if not isinstance(permissions, dict) or permissions.get("push") is not True:
            raise GitHubPublicationError("github_push_not_authorized")
        changed_paths = {
            str(item.get("path") or "") for item in [*additions, *deletions]
        }
        if (
            any(path.startswith(".github/workflows/") for path in changed_paths)
            and "workflow" not in scopes
        ):
            raise GitHubPublicationError("github_workflow_scope_required")

        # Recover the narrow crash window after GitHub created the PR but before
        # Chronos persisted provider evidence. Do this before the stale-base
        # guard: the base may legitimately advance after the already-created PR.
        persisted_commit = str(state.get("provider_commit_oid") or "")
        if re.fullmatch(r"[0-9a-f]{40}", persisted_commit):
            early_params = {
                "state": "all",
                "head": f"{owner}:{head}",
                "base": base,
                "per_page": 100,
            }
            early_status, early_rows, _ = await self._request(
                "GET", f"{repo_path}/pulls", params=early_params
            )
            if early_status == 200 and isinstance(early_rows, list):
                early_pull = self._find_pull(early_rows, marker)
                if early_pull is not None:
                    provider = self._validate_pr_evidence(
                        early_pull,
                        owner=owner,
                        repo=repo,
                        commit_oid=persisted_commit,
                    )
                    state = {
                        **state,
                        "stage": "pr_created",
                        "provider_pr_id": provider["id"],
                        "provider_node_id": provider["node_id"],
                        "provider_number": provider["number"],
                        "provider_commit_oid": provider["commit_oid"],
                        "provider_url": provider["url"],
                    }
                    await persist(state)
                    return provider, state, False

        base_ref_path = f"{repo_path}/git/ref/heads/{quote(base, safe='')}"
        status, base_ref, _ = await self._request("GET", base_ref_path)
        if status == 404:
            raise GitHubPublicationError("github_base_not_found")
        if status != 200 or not isinstance(base_ref, dict):
            raise GitHubPublicationError("github_provider_unavailable")
        base_object = base_ref.get("object")
        base_sha = str(base_object.get("sha") if isinstance(base_object, dict) else "")
        if base_sha != source_sha:
            raise GitHubPublicationError("github_base_moved")

        head_ref_path = f"{repo_path}/git/ref/heads/{quote(head, safe='')}"
        status, head_ref, _ = await self._request("GET", head_ref_path)
        if status == 404:
            create_status, created_ref, _ = await self._request(
                "POST",
                f"{repo_path}/git/refs",
                payload={"ref": f"refs/heads/{head}", "sha": base_sha},
            )
            if create_status not in {201, 422}:
                raise GitHubPublicationError("github_provider_rejected")
            if create_status == 422:
                get_status, created_ref, _ = await self._request("GET", head_ref_path)
                if get_status != 200:
                    raise GitHubPublicationError("github_provider_rejected")
            head_ref = created_ref
            state = {**state, "stage": "branch_created", "base_sha": base_sha}
            await persist(state)
        elif status != 200:
            raise GitHubPublicationError("github_provider_unavailable")
        head_object = head_ref.get("object") if isinstance(head_ref, dict) else None
        head_sha = str(head_object.get("sha") if isinstance(head_object, dict) else "")
        if re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
            raise GitHubPublicationError("github_provider_unavailable")

        commit_oid = str(state.get("provider_commit_oid") or "")
        if head_sha != base_sha:
            if commit_oid != head_sha:
                commit_status, commit_info, _ = await self._request(
                    "GET", f"{repo_path}/git/commits/{head_sha}"
                )
                message = str(
                    commit_info.get("message") if isinstance(commit_info, dict) else ""
                )
                if commit_status == 200 and marker in message:
                    commit_oid = head_sha
                    state = {
                        **state,
                        "stage": "commit_created",
                        "provider_commit_oid": commit_oid,
                    }
                    await persist(state)
                elif state.get("stage") in {"branch_created", "claimed"}:
                    raise GitHubPublicationError("github_head_exists")
                else:
                    raise GitHubPublicationError("github_branch_changed")

        if not commit_oid:
            mutation = """
mutation CreateChronosCommit($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) {
    commit { oid url }
    ref { name }
  }
}
""".strip()
            commit_response_status, commit_response, _ = await self._request(
                "POST",
                "/graphql",
                payload={
                    "query": mutation,
                    "variables": {
                        "input": {
                            "branch": {
                                "repositoryNameWithOwner": f"{owner}/{repo}",
                                "branchName": head,
                            },
                            "message": {
                                "headline": title,
                                "body": f"Approved Chronos publication\n\n[{marker}]",
                            },
                            "fileChanges": {
                                "additions": additions,
                                "deletions": deletions,
                            },
                            "expectedHeadOid": head_sha,
                            "clientMutationId": key_hash,
                        }
                    },
                },
            )
            if commit_response_status != 200 or not isinstance(commit_response, dict):
                raise GitHubPublicationError("github_provider_rejected")
            if commit_response.get("errors"):
                raise GitHubPublicationError("github_branch_changed")
            data = commit_response.get("data")
            commit_payload = (
                data.get("createCommitOnBranch") if isinstance(data, dict) else None
            )
            commit = commit_payload.get("commit") if isinstance(commit_payload, dict) else None
            commit_oid = str(commit.get("oid") if isinstance(commit, dict) else "")
            if re.fullmatch(r"[0-9a-f]{40}", commit_oid) is None:
                raise GitHubPublicationError("github_provider_unavailable")
            state = {
                **state,
                "stage": "commit_created",
                "provider_commit_oid": commit_oid,
            }
            await persist(state)

        pr_body = f"{body.rstrip()}\n\n<!-- {marker} -->".strip()
        list_params = {
            "state": "all",
            "head": f"{owner}:{head}",
            "base": base,
            "per_page": 100,
        }
        status, pull_rows, _ = await self._request(
            "GET", f"{repo_path}/pulls", params=list_params
        )
        if status != 200 or not isinstance(pull_rows, list):
            raise GitHubPublicationError("github_provider_unavailable")
        pull = self._find_pull(pull_rows, marker)
        newly_published = False
        if pull is None:
            status, created_pull, _ = await self._request(
                "POST",
                f"{repo_path}/pulls",
                payload={
                    "title": title,
                    "body": pr_body,
                    "head": head,
                    "base": base,
                    "maintainer_can_modify": True,
                },
            )
            if status == 201 and isinstance(created_pull, dict):
                pull = created_pull
                newly_published = True
            elif status == 422:
                list_status, pull_rows, _ = await self._request(
                    "GET", f"{repo_path}/pulls", params=list_params
                )
                if list_status == 200 and isinstance(pull_rows, list):
                    pull = self._find_pull(pull_rows, marker)
            if pull is None:
                raise GitHubPublicationError("github_provider_rejected")

        provider = self._validate_pr_evidence(
            pull, owner=owner, repo=repo, commit_oid=commit_oid
        )
        state = {
            **state,
            "stage": "pr_created",
            "provider_pr_id": provider["id"],
            "provider_node_id": provider["node_id"],
            "provider_number": provider["number"],
            "provider_commit_oid": provider["commit_oid"],
            "provider_url": provider["url"],
        }
        await persist(state)
        return provider, state, newly_published

    @staticmethod
    def _find_pull(rows: list[Any], marker: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in rows
                if isinstance(row, dict) and marker in str(row.get("body") or "")
            ),
            None,
        )

    @staticmethod
    def _validate_pr_evidence(
        pull: dict[str, Any], *, owner: str, repo: str, commit_oid: str
    ) -> dict[str, Any]:
        pr_id = pull.get("id")
        node_id = str(pull.get("node_id") or "")
        number = pull.get("number")
        url = str(pull.get("html_url") or "")
        expected_url = f"https://github.com/{owner}/{repo}/pull/{number}"
        if (
            not isinstance(pr_id, int)
            or not isinstance(number, int)
            or not node_id
            or url.lower() != expected_url.lower()
            or re.fullmatch(r"[0-9a-f]{40}", commit_oid) is None
        ):
            raise GitHubPublicationError("github_provider_unavailable")
        return {
            "id": pr_id,
            "node_id": node_id,
            "number": number,
            "commit_oid": commit_oid,
            "url": url,
        }
