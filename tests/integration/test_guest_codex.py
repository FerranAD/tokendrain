from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from tokendrain.codex import RpcError
from tokendrain_guestd.daemon import GuestDaemon


@pytest.fixture
def fake_codex(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!"
        + sys.executable
        + "\n"
        + """
import json, os, sys
from pathlib import Path
home = Path(os.environ["CODEX_HOME"])
start_count = home / "starts"
start_count.write_text(str(int(start_count.read_text()) + 1) if start_count.exists() else "1")
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    params = request.get("params", {})
    if "id" not in request:
        continue
    result = {}
    if method == "initialize":
        result = {"userAgent": "fake-codex"}
    elif method in ("thread/start", "thread/resume"):
        result = {"thread": {"id": params.get("threadId", "persistent-thread")}}
        (home / "thread").write_text(result["thread"]["id"])
    elif method == "turn/start":
        result = {"turn": {"id": "turn-1", "status": "inProgress"}}
    elif method == "turn/interrupt":
        notification = {"method":"turn/completed",
                        "params":{"turn":{"id":"turn-1", "status":"interrupted"}}}
        print(json.dumps(notification), flush=True)
    elif method == "environment/read":
        result = {"github":os.environ.get("GH_TOKEN"),
                  "access":None,
                  "secret":os.environ.get("EXAMPLE_SECRET")}
    print(json.dumps({"id":request["id"], "result":result}), flush=True)
"""
    )
    executable.chmod(0o700)
    return executable


def runtime(token: str) -> dict[str, object]:
    return {"mode": "chatgpt", "access_token": token, "expires_at": time.time() + 3600}


async def test_guest_process_rotation_resumes_without_replaying_turn(
    tmp_path: Path, fake_codex: Path
) -> None:
    guest = GuestDaemon(
        tmp_path / "run", tmp_path / "persist", tmp_path / "workspace", str(fake_codex)
    )
    try:
        await guest.handle(
            "credentials_set",
            {
                "openai": runtime("first-token"),
                "github_token": "github-one",
                "secrets": {"EXAMPLE_SECRET": "runtime-only"},
            },
        )
        assert await guest.handle("codex_start", {}) == {"userAgent": "fake-codex"}
        result = await guest.handle(
            "codex_rpc_request",
            {"method": "thread/start", "params": {"model": "test", "approvalPolicy": "never"}},
        )
        thread_id = result["thread"]["id"]
        await guest.handle(
            "codex_rpc_request", {"method": "turn/start", "params": {"threadId": thread_id}}
        )
        with pytest.raises(RpcError, match="boundary"):
            await guest.handle("openai_token_rotate", runtime("too-soon"))
        await guest.handle(
            "codex_rpc_request",
            {"method": "turn/interrupt", "params": {"threadId": thread_id, "turnId": "turn-1"}},
        )
        await asyncio.wait_for(guest.turn_idle.wait(), 2)
        rotated = await guest.handle("openai_token_rotate", runtime("second-token"))
        assert rotated == {"thread_id": thread_id, "restarted": True}
        assert (tmp_path / "persist" / "starts").read_text() == "2"
        assert (tmp_path / "persist" / "thread").read_text() == thread_id
        environment = await guest.handle("codex_rpc_request", {"method": "environment/read"})
        assert environment == {
            "access": None,
            "github": "github-one",
            "secret": "runtime-only",
        }
        await guest.handle("github_token_rotate", {"token": "github-two"})
        environment = await guest.handle("codex_rpc_request", {"method": "environment/read"})
        assert environment["github"] == "github-two"
        assert (tmp_path / "run" / "github-token").read_text() == "github-two"
        # Guest-managed credentials are never written in the persistent Codex home.
        assert not (tmp_path / "persist" / "auth.json").exists()
        assert "runtime-only" not in json.dumps(
            [path.read_text() for path in (tmp_path / "persist").iterdir()]
        )
        await guest.handle("credentials_clear", {})
        assert not list((tmp_path / "run").iterdir())
        assert guest.credentials is None
    finally:
        await guest.stop_codex()


async def test_guest_rejects_reserved_secret_environment(tmp_path: Path) -> None:
    guest = GuestDaemon(tmp_path / "run", tmp_path / "persist", tmp_path / "workspace")
    with pytest.raises(ValueError, match="reserved"):
        await guest.handle(
            "credentials_set",
            {"openai": runtime("token"), "secrets": {"CODEX_HOME": "/workspace/leak"}},
        )


async def test_codex_crash_propagates_as_guest_failure(tmp_path: Path, fake_codex: Path) -> None:
    guest = GuestDaemon(
        tmp_path / "run", tmp_path / "persist", tmp_path / "workspace", str(fake_codex)
    )
    try:
        await guest.handle("credentials_set", {"openai": runtime("token")})
        await guest.handle("codex_start", {})
        assert guest.process
        guest.process.kill()
        await asyncio.wait_for(guest.failed.wait(), 2)
        assert guest.codex and guest.codex.closed.is_set()
    finally:
        await guest.stop_codex()
    assert not guest._tasks


async def test_event_forwarder_failure_is_observed(tmp_path: Path, fake_codex: Path) -> None:
    class BrokenEventGuest(GuestDaemon):
        async def _forward_notifications(self) -> None:
            raise RuntimeError("broken notification channel")

    guest = BrokenEventGuest(
        tmp_path / "run", tmp_path / "persist", tmp_path / "workspace", str(fake_codex)
    )
    try:
        await guest.handle("credentials_set", {"openai": runtime("token")})
        await guest.handle("codex_start", {})
        await asyncio.wait_for(guest.failed.wait(), 2)
    finally:
        await guest.stop_codex()
    assert not guest._tasks


def test_redaction_preserves_json_structure(tmp_path: Path) -> None:
    guest = GuestDaemon(tmp_path / "run", tmp_path / "persist", tmp_path / "workspace")
    guest.redactor.replace_values(['sensitive"value', "nested-secret"])
    original = {"method": "event", "text": 'sensitive"value', "nested": ["nested-secret", 5]}
    redacted = guest._redact_event(original)
    assert redacted == {"method": "event", "text": "[REDACTED]", "nested": ["[REDACTED]", 5]}
    assert json.loads(json.dumps(redacted)) == redacted


async def test_guest_shutdown_closes_live_connection_before_server_wait(
    tmp_path: Path,
) -> None:
    """Exercise the complete guest process; systemctl is a harmless local fixture."""
    import os

    from tokendrain.protocol import FramedPeer

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    called = tmp_path / "systemctl-called"
    systemctl.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(called)!r}).write_text(' '.join(sys.argv[1:]))\n"
    )
    systemctl.chmod(0o700)
    socket_path = tmp_path / "guest.sock"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "tokendrain_guestd.main",
        "--unix-socket",
        str(socket_path),
        "--runtime-dir",
        str(tmp_path / "runtime"),
        "--codex-home",
        str(tmp_path / "codex"),
        "--workspace",
        str(tmp_path / "workspace"),
        "--poweroff-on-shutdown",
        env={**os.environ, "PATH": str(bin_dir)},
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    peer = None
    try:
        async with asyncio.timeout(5):
            while True:
                try:
                    reader, writer = await asyncio.open_unix_connection(socket_path)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    await asyncio.sleep(0.01)
        peer = FramedPeer(reader, writer)
        await peer.start()
        assert await peer.request("shutdown") == {}
        # Deliberately leave the client connection open. The server must close
        # it itself, not wait indefinitely for its caller to close first.
        _, stderr = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, stderr.decode()
        assert called.read_text() == "--no-block reboot"
    finally:
        if peer:
            await peer.close()
        if process.returncode is None:
            process.kill()
            await process.wait()
