"""Shared, serialized account metadata probes for the UI and host workers."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from tokendrain.auth.probe import AuthProbe
from tokendrain.codex.rpc import RpcError
from tokendrain.usage import normalize_rate_limits

if TYPE_CHECKING:
    from tokendrain.application import Application


async def account_probe(services: Application, *, force: bool = False) -> dict[str, Any]:
    assert services.probe_lock is not None
    async with services.probe_lock:
        cached = services.probe_cache
        if not force and cached and time.time() - float(str(cached["observed_at"])) < 60:
            return cached
        accounts = await services.auth.accounts()
        if not any(account.connected for account in accounts):
            return {
                "models": [],
                "usage": {},
                "observed_at": time.time(),
                "usage_error": "Connect OpenAI to observe usage.",
            }
        try:
            result = await AuthProbe(
                services.auth, services.http, services.settings.auth_runtime_dir
            ).read()
            value = result.model_dump()
        except (RpcError, ValueError, OSError, TimeoutError, httpx.HTTPError) as exc:
            logging.getLogger(__name__).warning(
                "Codex metadata probe unavailable (%s)", type(exc).__name__
            )
            value = {
                "models": (cached or {}).get("models", []),
                "usage": {},
                "observed_at": time.time(),
                "usage_error": "Codex metadata unavailable. Check the host Codex process "
                "and account credentials in Settings; usage will be retried shortly.",
            }
        # Cache failures too: page refreshes must not launch repeated failing probes.
        # Keep historical observations untouched instead of manufacturing zero usage.
        services.probe_cache = value
        windows = normalize_rate_limits(value["usage"])
        if windows:
            await services.runs.observe(windows)
        return value
