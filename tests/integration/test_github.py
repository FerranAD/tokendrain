"""Focused security and lifecycle checks for the documented GitHub REST contracts."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select
from test_api import api, new_project  # noqa: F401

from tokendrain.db.models import GitHubApp, GitHubPolicy, Setting
from tokendrain.github.policy import protection_verified, ruleset_payload
from tokendrain.github.provider import (
    APP_PERMISSIONS,
    InstallationToken,
    IntegrationInput,
    app_manifest,
)
from tokendrain.orchestration.driver import guest_github_secret


@pytest.fixture
async def github(api):  # noqa: F811
    client, services = api
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    await services.credentials.put("test-github-key", private)
    state = {"ruleset": None, "creates": 0, "deletes": 0, "reject": False, "requests": []}

    def handler(request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        state["requests"].append((path, method, body))
        if path.startswith("/app-manifests/"):
            assert "authorization" not in request.headers
            return httpx.Response(
                201,
                json={
                    "id": 123,
                    "slug": "tokendrain-test",
                    "pem": private.decode(),
                    "client_secret": "discard-client-secret",
                    "webhook_secret": "discard-webhook-secret",
                },
            )
        if path == "/app/installations":
            return httpx.Response(
                200, json=[{"id": 7, "account": {"login": "owner"}, "permissions": APP_PERMISSIONS}]
            )
        if path == "/app/installations/7/access_tokens":
            permissions = body["permissions"]
            if permissions != {"metadata": "read"}:
                assert body["repository_ids"] == [99]
            return httpx.Response(
                201,
                json={
                    "token": "host-policy" if "administration" in permissions else "guest-scoped",
                    "permissions": permissions,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
        if path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": 99,
                            "full_name": "owner/repo",
                            "private": True,
                            "default_branch": "trunk",
                        }
                    ]
                },
            )
        if path == "/repositories/99":
            return httpx.Response(200, json={"id": 99, "full_name": "owner/repo"})
        assert path.startswith("/repos/owner/repo/rulesets")
        assert request.headers["authorization"] == "Bearer host-policy"
        if path.endswith("/rulesets") and method == "GET":
            return httpx.Response(200, json=[state["ruleset"]] if state["ruleset"] else [])
        if method in {"POST", "PUT"}:
            assert body == ruleset_payload()
            if state["reject"]:
                return httpx.Response(403, json={"message": "Upgrade your plan to use rulesets."})
            if method == "POST":
                state["creates"] += 1
            state["ruleset"] = {**body, "id": 42, "source_type": "Repository"}
            if state.pop("drop_create_response", False):
                raise httpx.ReadTimeout("Response lost after creation", request=request)
            return httpx.Response(201, json=state["ruleset"])
        if method == "DELETE":
            state["deletes"] += 1
            state["ruleset"] = None
            return httpx.Response(204)
        return (
            httpx.Response(200, json=state["ruleset"])
            if state["ruleset"]
            else httpx.Response(404, json={"message": "Not Found"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        services.github.http = upstream
        async with services.sessions.begin() as db:
            db.add(
                GitHubApp(
                    id=1, app_id="123", slug="tokendrain-test", credential_ref="test-github-key"
                )
            )
        assert (await client.post("/api/v1/integrations/github/sync")).status_code == 200
        yield client, services, state


def test_manifest_private_url_and_guest_boundary():
    manifest = app_manifest("https://drain.tail123.ts.net", "tokendrain-test")
    assert manifest["redirect_url"].startswith("https://drain.tail123.ts.net/")
    assert manifest["setup_url"].startswith("https://drain.tail123.ts.net/")
    assert manifest["hook_attributes"]["active"] is False
    assert not manifest["default_events"] and manifest["request_oauth_on_install"] is False
    for mode in ("read_only", "pull_requests", "direct_write"):
        binding = IntegrationInput(
            installation_id=7, repository_id=99, repository_name="o/r", access_mode=mode
        )
        assert "administration" not in binding.permissions
        assert "workflows" not in binding.permissions
        if mode == "read_only":
            assert set(binding.permissions.values()) == {"read"}
    token = InstallationToken(
        token="host-secret", expires_at=datetime.now(UTC), permissions={"administration": "write"}
    )
    with pytest.raises(ValueError, match="never be sent to guestd"):
        guest_github_secret(token)
    unsafe = ruleset_payload() | {
        "bypass_actors": [{"actor_type": "Integration", "actor_id": 123, "bypass_mode": "always"}]
    }
    assert not protection_verified(unsafe)


async def test_manifest_state_browser_binding_expiry_and_replay(github):
    client, services, state = github
    setup = (await client.post("/api/v1/integrations/github/connect", json={})).json()
    value = parse_qs(urlsplit(setup["action"]).query)["state"][0]
    assert setup["state"] == value
    assert (
        await client.get(
            "/api/v1/integrations/github/callback", params={"code": "test", "state": "forged"}
        )
    ).status_code == 400
    callback = "/api/v1/integrations/github/callback?code=test&state=" + value
    browser_cookie = client.cookies.get("tokendrain_github_manifest")
    client.cookies.delete("tokendrain_github_manifest")
    assert (await client.get(callback)).status_code == 400
    client.cookies.set("tokendrain_github_manifest", browser_cookie)
    result = await client.get(callback)
    assert result.status_code == 303, result.text
    assert result.headers["location"] == "https://github.com/apps/tokendrain-test/installations/new"
    assert (await client.get(callback)).status_code == 400
    assert (
        len([request for request in state["requests"] if request[0].startswith("/app-manifests")])
        == 1
    )
    status = (await client.get("/api/v1/integrations/github")).text
    assert (
        "PRIVATE KEY" not in status and "app_id" not in status and "installation_id" not in status
    )
    setup = (await client.post("/api/v1/integrations/github/connect", json={})).json()
    async with services.sessions.begin() as db:
        pending = await db.get(Setting, "github_manifest")
        pending.value = {**pending.value, "expires_at": 0}
    assert (
        await client.get(
            "/api/v1/integrations/github/callback", params={"code": "test", "state": setup["state"]}
        )
    ).status_code == 400
    # The manual credential setup endpoint has been removed.
    assert (await client.put("/api/v1/integrations/github", json={})).status_code == 405


async def test_shared_ruleset_conflicts_cleanup_and_scoped_tokens(github):
    client, services, state = github
    first, second, third = [
        await new_project(client, name) for name in ("Project A", "Project B", "Project C")
    ]
    binding = {"repository_id": 99, "access_mode": "pull_requests"}

    async def attach(project, mode):
        return await client.put(
            f"/api/v1/projects/{project}/github", json={**binding, "access_mode": mode}
        )

    assert (await attach(first, "pull_requests")).status_code == 200
    assert (await attach(second, "pull_requests")).status_code == 200
    assert state["creates"] == 1
    assert protection_verified(state["ruleset"])
    # A user edit that adds App bypass must be removed before another writable Run.
    state["ruleset"]["bypass_actors"].append(
        {"actor_type": "Integration", "actor_id": 123, "bypass_mode": "always"}
    )
    assert (await attach(second, "pull_requests")).status_code == 200
    assert protection_verified(state["ruleset"])
    denied = await attach(third, "direct_write")
    assert denied.status_code == 409
    assert denied.json()["detail"]["conflicting_project_name"] in {"Project A", "Project B"}
    denied_create = await client.post(
        "/api/v1/projects",
        json={"name": "Conflict", "github": {**binding, "access_mode": "direct_write"}},
    )
    assert denied_create.status_code == 409
    inventory = (await client.get("/api/v1/integrations/github/repositories")).json()[0]
    assert len(inventory["used_by"]) == 2 and "installation_id" not in inventory
    assert (await attach(third, "read_only")).status_code == 200
    for mode in ("read_only", "pull_requests", "direct_write"):
        token = await services.github.token(
            "123",
            "test-github-key",
            IntegrationInput(
                installation_id=7, repository_id=99, repository_name="owner/repo", access_mode=mode
            ),
        )
        assert guest_github_secret(token) == "guest-scoped"
        assert "administration" not in token.permissions
    assert (await client.delete(f"/api/v1/projects/{first}/github")).status_code == 204
    assert state["deletes"] == 0
    assert (await attach(second, "direct_write")).status_code == 200  # excludes edited project
    assert state["deletes"] == 1
    assert (await attach(first, "pull_requests")).status_code == 409
    assert (await client.delete(f"/api/v1/projects/{second}")).status_code == 204
    # Concurrent incompatible creates must serialize into one success and one conflict.
    outcomes = await asyncio.gather(
        *(
            client.post(
                "/api/v1/projects",
                json={"name": mode, "github": {"repository_id": 99, "access_mode": mode}},
            )
            for mode in ("pull_requests", "direct_write")
        )
    )
    assert sorted(response.status_code for response in outcomes) == [201, 409]
    winner = next(response.json()["id"] for response in outcomes if response.status_code == 201)
    assert (await client.delete(f"/api/v1/projects/{winner}")).status_code == 204
    assert (await attach(first, "pull_requests")).status_code == 200
    deletes = state["deletes"]
    assert (await client.delete(f"/api/v1/projects/{first}")).status_code == 204
    assert state["deletes"] == deletes + 1
    assert (await client.delete("/api/v1/integrations/github")).status_code == 204


async def test_ruleset_capability_failure_keeps_read_only_and_explicit_direct_write(github):
    from tokendrain.github.policy import reconcile_repository

    client, services, state = github
    state["reject"] = True
    project = await new_project(client)
    result = await client.put(f"/api/v1/projects/{project}/github", json={"repository_id": 99})
    assert result.status_code == 200
    assert "Upgrade your plan" in result.json()["policy_error"]
    with pytest.raises(ValueError, match="could not enforce PR-only"):
        await reconcile_repository(services.sessions, services.github, 99)
    async with services.sessions() as db:
        assert (await db.scalar(select(GitHubPolicy))).error
    from unittest.mock import AsyncMock

    from tokendrain.domain import RunTemplate

    vm_start = AsyncMock(side_effect=AssertionError("Unprotected VM must not start"))
    services.supervisor.vm.start = vm_start
    run = await services.runs.create(RunTemplate(projects=[{"project_id": project}]))
    await services.supervisor.execute(run["executions"][0]["id"], asyncio.Event())
    vm_start.assert_not_called()
    failed = (await services.runs.get(run["id"]))["executions"][0]
    assert failed["status"] == "failed" and "could not enforce PR-only" in failed["error"]
    assert not any(
        body and body.get("permissions", {}).get("contents") == "write"
        for _, _, body in state["requests"]
    )
    readonly = await client.put(
        f"/api/v1/projects/{project}/github", json={"repository_id": 99, "access_mode": "read_only"}
    )
    assert readonly.status_code == 200 and readonly.json()["policy_error"] is None
    direct = await client.put(
        f"/api/v1/projects/{project}/github",
        json={"repository_id": 99, "access_mode": "direct_write"},
    )
    assert direct.status_code == 200 and direct.json()["policy_error"] is None


async def test_uncertain_rule_creation_is_durable_and_never_weakens_unknown_rules(github):
    from tokendrain.github.policy import reconcile_repository

    client, services, state = github
    state["drop_create_response"] = True
    project = await new_project(client)
    result = await client.put(f"/api/v1/projects/{project}/github", json={"repository_id": 99})
    assert result.status_code == 200 and result.json()["policy_error"]
    async with services.sessions() as db:
        policy = await db.get(GitHubPolicy, 99)
        assert policy.may_exist and policy.ruleset_id is None
    assert state["creates"] == 1
    # Unknown ownership requires recovery, never another create or an arbitrary delete.
    with pytest.raises(ValueError, match="untracked tokendrain ruleset"):
        await reconcile_repository(services.sessions, services.github, 99)
    direct = await client.put(
        f"/api/v1/projects/{project}/github",
        json={"repository_id": 99, "access_mode": "direct_write"},
    )
    assert direct.status_code == 200 and "untracked" in direct.json()["policy_error"]
    with pytest.raises(ValueError, match="untracked tokendrain ruleset"):
        await reconcile_repository(services.sessions, services.github, 99)
    assert state["creates"] == 1 and state["deletes"] == 0
