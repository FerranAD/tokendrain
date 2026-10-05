"""Reconcile only tracked tokendrain rules. SQLite serializes policy and binding changes."""

import asyncio
from typing import Any

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tokendrain.db.models import GitHubApp, GitHubPolicy, ProjectGitHub
from tokendrain.github.provider import GitHubProvider, IntegrationInput

RULESET_NAME = "tokendrain: PR-only"


class GitHubPolicyError(ValueError):
    """Writable execution must stop until repository protection is reconciled."""


async def reserve_pr_policy(db: AsyncSession, binding: IntegrationInput | ProjectGitHub) -> None:
    """Commit ownership intent with desired state before any external rule creation.

    The reconciler commits an uncertain-create marker on this row before calling
    GitHub. After a hard crash, Direct write inspects any potentially orphaned rule
    rather than skipping cleanup because a local transaction rolled back.
    """
    if binding.access_mode == "pull_requests" and not await db.get(
        GitHubPolicy, binding.repository_id
    ):
        db.add(
            GitHubPolicy(
                repository_id=binding.repository_id,
                installation_id=binding.installation_id,
                repository_name=binding.repository_name,
                may_exist=False,
            )
        )
        await db.flush()


def ruleset_payload() -> dict[str, Any]:
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        # GitHub's built-in repository admin role. Applies to personal and org repositories.
        # An installation is an Integration actor, not a human repository role.
        "bypass_actors": [{"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always"}],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                },
            },
        ],
    }


def protection_verified(value: dict[str, Any]) -> bool:
    desired = ruleset_payload()
    return all(
        value.get(key) == desired[key]
        for key in ("name", "target", "enforcement", "bypass_actors", "conditions")
    ) and {r.get("type") for r in value.get("rules", [])} >= {
        "pull_request",
        "deletion",
        "non_fast_forward",
    }


def github_reason(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        try:
            reason = error.response.json().get("message", "GitHub rejected the request")
        except ValueError:
            reason = "GitHub rejected the request"
        return f"GitHub {error.response.status_code}: {str(reason)[:500]}"
    if isinstance(error, httpx.HTTPError):
        return "Could not connect to GitHub; check outbound HTTPS access."
    return str(error)[:500]


async def reconcile_repository(
    sessions: async_sessionmaker[AsyncSession],
    provider: GitHubProvider,
    repository_id: int,
    *,
    remove_owned: bool = False,
) -> None:
    failure: str | None = None
    cancelled = False
    async with sessions() as db:
        # Same serialization boundary as binding mutations. A concurrent mode change cannot
        # remove protection while another project is adding PR-only desired state.
        await db.execute(text("BEGIN IMMEDIATE"))
        bindings = list(
            (
                await db.scalars(
                    select(ProjectGitHub).where(ProjectGitHub.repository_id == repository_id)
                )
            ).all()
        )
        needed = not remove_owned and any(row.access_mode == "pull_requests" for row in bindings)
        policy = await db.get(GitHubPolicy, repository_id)
        if not needed and not policy:
            return
        if not needed and policy and not policy.ruleset_id and not policy.may_exist:
            await db.delete(policy)
            await db.commit()
            return
        if policy is None:
            binding = bindings[0]
            policy = GitHubPolicy(
                repository_id=repository_id,
                installation_id=binding.installation_id,
                repository_name=binding.repository_name,
            )
            db.add(policy)
        if bindings:
            policy.installation_id = bindings[0].installation_id
        try:
            app = await db.get(GitHubApp, 1)
            if app is None:
                raise ValueError("Connect GitHub before reconciling repository protection")
            bearer = await provider.policy_token(
                app.app_id, app.credential_ref, policy.installation_id, repository_id
            )
            # Resolve by immutable ID, including repository renames/transfers.
            repository = await provider._request("GET", f"/repositories/{repository_id}", bearer)
            if repository["id"] != repository_id:
                raise ValueError("GitHub repository identity changed")
            policy.repository_name = repository["full_name"]
            path = f"/repos/{policy.repository_name}/rulesets"
            existing = None
            if policy.ruleset_id:
                try:
                    existing = await provider._request("GET", f"{path}/{policy.ruleset_id}", bearer)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 404:
                        raise
                if existing and (
                    existing.get("name") != RULESET_NAME
                    or existing.get("source_type") != "Repository"
                ):
                    raise ValueError(
                        "Tracked ruleset no longer belongs to tokendrain; inspect repository rules"
                    )
                if existing is None:
                    policy.ruleset_id = None
            # Never adopt a user's rule based solely on its name. Also detect orphaned
            # creates after a process/DB failure rather than creating duplicates.
            if policy.ruleset_id is None:
                for page in range(1, 1001):
                    rules = await provider._request(
                        "GET", f"{path}?includes_parents=false&per_page=100&page={page}", bearer
                    )
                    if any(rule.get("name") == RULESET_NAME for rule in rules):
                        raise ValueError(
                            "An untracked tokendrain ruleset already exists; "
                            "inspect repository rules before retrying"
                        )
                    if len(rules) < 100:
                        break
                else:
                    raise ValueError("GitHub ruleset pagination limit exceeded")
            if needed:
                if policy.ruleset_id is None:
                    # Commit the uncertain-create marker BEFORE the external request.
                    # If the process dies after GitHub creates the rule, Direct write
                    # must still inspect it rather than assume no managed rule exists.
                    policy.may_exist = True
                    app_ref = app.credential_ref
                    await db.commit()
                    await db.execute(text("BEGIN IMMEDIATE"))
                    needed_now = await db.scalar(
                        select(ProjectGitHub.project_id)
                        .where(
                            ProjectGitHub.repository_id == repository_id,
                            ProjectGitHub.access_mode == "pull_requests",
                        )
                        .limit(1)
                    )
                    if not needed_now:
                        # A mode change may have raced with another successful create.
                        # Re-read and clean up its tracked rule; never discard its ID.
                        await db.rollback()
                        await reconcile_repository(sessions, provider, repository_id)
                        return
                    await db.refresh(app)
                    if app.credential_ref != app_ref:
                        policy.may_exist = False
                        raise ValueError(
                            "GitHub connection changed; retry repository reconciliation"
                        )
                    # Another reconciler may have completed creation in the brief
                    # commit/reacquire interval. Reuse its durable ID.
                    await db.refresh(policy)
                    if policy.ruleset_id is None:
                        try:
                            created = await provider._request(
                                "POST", path, bearer, ruleset_payload()
                            )
                        except httpx.HTTPStatusError as error:
                            # Definite rejection created no ruleset. Explicit Direct write
                            # remains available on plans that cannot enforce PR-only.
                            if error.response.status_code in {400, 401, 403, 404, 422}:
                                policy.may_exist = False
                            raise
                    else:
                        created = {"id": policy.ruleset_id}
                    policy.ruleset_id = created["id"]
                elif not protection_verified(existing or {}):
                    await provider._request(
                        "PUT", f"{path}/{policy.ruleset_id}", bearer, ruleset_payload()
                    )
                verified = await provider._request("GET", f"{path}/{policy.ruleset_id}", bearer)
                if not protection_verified(verified):
                    raise ValueError(
                        "GitHub did not activate the required default-branch protection"
                    )
                policy.error = None
            else:
                if policy.ruleset_id:
                    await provider._request("DELETE", f"{path}/{policy.ruleset_id}", bearer)
                    # Do not promise direct write until deletion is confirmed.
                    try:
                        await provider._request("GET", f"{path}/{policy.ruleset_id}", bearer)
                    except httpx.HTTPStatusError as error:
                        if error.response.status_code != 404:
                            raise
                    else:
                        raise ValueError("GitHub has not removed the tokendrain PR-only rule")
                await db.delete(policy)
        except (ValueError, httpx.HTTPError) as error:
            prefix = "enforce PR-only protection" if needed else "reconcile repository protection"
            failure = (
                f"Tokendrain could not {prefix} for {policy.repository_name}. "
                f"{github_reason(error)}"
            )
            policy.error = failure
        except asyncio.CancelledError:
            # Persist uncertain external creation for safe recovery, even on cancellation.
            policy.error = "Repository policy reconciliation was interrupted; retry before writing."
            cancelled = True
        await db.commit()
    if cancelled:
        raise asyncio.CancelledError
    if failure:
        raise GitHubPolicyError(failure)


async def reconcile_all(
    sessions: async_sessionmaker[AsyncSession],
    provider: GitHubProvider,
    *,
    remove_owned: bool = False,
) -> None:
    async with sessions() as db:
        ids = set((await db.scalars(select(ProjectGitHub.repository_id))).all())
        ids.update((await db.scalars(select(GitHubPolicy.repository_id))).all())
    failures = []
    for repository_id in sorted(ids):
        try:
            await reconcile_repository(sessions, provider, repository_id, remove_owned=remove_owned)
        except ValueError as error:
            failures.append(str(error))
    if failures:
        raise ValueError("\n".join(failures))
