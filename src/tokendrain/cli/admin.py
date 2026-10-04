"""Small administrative client for the Tokendrain daemon."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

import httpx
from pydantic import ValidationError

from tokendrain.config import Settings
from tokendrain.doctor import DoctorCheck, inspect_checks


def platform_settings(path: Path = Path("/etc/tokendrain/platform.json")) -> Settings:
    defaults = json.loads(path.read_text()) if path.is_file() else {}
    if not isinstance(defaults, dict):
        raise ValueError("Platform configuration must contain a JSON object")
    defaults = {
        name: value
        for name, value in defaults.items()
        if f"TOKENDRAIN_{name.upper()}" not in os.environ
    }
    return Settings(**defaults)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--url", help="Daemon URL; defaults to its local listening address")
    result.add_argument("--state-dir", type=Path)
    result.add_argument("--token-file", type=Path)
    result.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "Show daemon and platform status"),
        ("projects", "List persistent projects"),
        ("runs", "List recent runs"),
        ("login-token", "Print the protected administrative token for browser login"),
        ("doctor", "Read-only KVM, storage, networking and daemon diagnostics"),
    ):
        commands.add_parser(name, help=help_text)
    return result


def token_path(settings: Settings, supplied: Path | None) -> Path:
    if supplied:
        return supplied
    if settings.admin_token_file:
        return settings.admin_token_file
    if settings.auth_mode == "none":
        return settings.state_dir / "unused-token"
    raise ValueError("Configure TOKENDRAIN_ADMIN_TOKEN_FILE or use --token-file")


def read_token(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        raise ValueError(f"Administrative token must have mode 0600 or 0400: {path}")
    value = path.read_text().strip()
    if len(value) < 32:
        raise ValueError("Administrative token file is empty or invalid")
    return value


def local_url(settings: Settings) -> str:
    host = settings.listen_address
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.port}"


async def fetch(client: httpx.AsyncClient, url: str, token: str, resource: str) -> Any:
    response = await client.get(
        url.rstrip("/") + "/api/v1/" + resource,
        headers={"Authorization": f"Bearer {token}"} if token else {},
        timeout=15,
    )
    if response.status_code == 401:
        raise ValueError("Daemon rejected the administrative token; verify --token-file")
    if response.is_error:
        raise ValueError(f"Daemon returned HTTP {response.status_code}")
    return response.json()


async def execute(
    arguments: argparse.Namespace,
    settings: Settings,
    client: httpx.AsyncClient,
    output: TextIO,
) -> int:
    if arguments.state_dir:
        settings.state_dir = arguments.state_dir
    path = token_path(settings, arguments.token_file)
    if arguments.command == "login-token":
        print(await asyncio.to_thread(read_token, path), file=output)
        return 0
    url = arguments.url or local_url(settings)
    if arguments.command == "doctor":
        checks = await asyncio.to_thread(inspect_checks, settings)
        try:
            token = (
                await asyncio.to_thread(read_token, path) if settings.auth_mode == "token" else ""
            )
            status = await fetch(client, url, token, "system")
            checks.append(
                DoctorCheck(
                    name="daemon_api", ok=True, scope="daemon", message=f"Authenticated: {url}"
                )
            )
            for check in status.get("checks", []):
                parsed = DoctorCheck.model_validate(check)
                if (
                    parsed.scope == "helper"
                    or parsed.name == "reconciliation"
                    or (parsed.scope == "daemon" and not parsed.ok)
                ):
                    checks.append(parsed)
        except (OSError, ValueError, httpx.HTTPError):
            checks.append(
                DoctorCheck(
                    name="daemon_api",
                    ok=False,
                    scope="daemon",
                    message=f"Cannot authenticate/reach {url}; token file {path}",
                )
            )
        if arguments.json:
            print(json.dumps([check.model_dump() for check in checks], indent=2), file=output)
        else:
            for check in checks:
                label = check.name if check.scope == "host" else f"{check.scope}.{check.name}"
                print(f"{'OK  ' if check.ok else 'FAIL'} {label}: {check.message}", file=output)
        return 0 if all(check.ok for check in checks) else 1
    token = await asyncio.to_thread(read_token, path) if settings.auth_mode == "token" else ""
    resource = "system" if arguments.command == "status" else arguments.command
    data = await fetch(client, url, token, resource)
    if arguments.json or not isinstance(data, list):
        print(json.dumps(data, indent=2), file=output)
    elif not data:
        print(f"No {arguments.command}.", file=output)
    else:
        for row in data:
            print(f"{row['id']}  {row.get('status', ''):12}  {row.get('name', '')}", file=output)
    return 0


async def run(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        settings = platform_settings()
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            return await execute(arguments, settings, client, sys.stdout)
    except (OSError, ValueError, httpx.HTTPError) as error:
        if isinstance(error, httpx.HTTPError):
            message = f"Could not contact daemon ({type(error).__name__})"
        elif isinstance(error, ValidationError):
            message = "Invalid TOKENDRAIN_* configuration"
        else:
            message = str(error)
        print(f"tokendrain: {message}", file=sys.stderr)
        return 1


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(asyncio.run(run(argv)))


if __name__ == "__main__":
    main()
