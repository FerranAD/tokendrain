from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import socket
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from tokendrain.auth.openai import RuntimeCredentials
from tokendrain.codex.client import CodexClient
from tokendrain.codex.rpc import JsonObject, JsonRpcPeer, RpcError
from tokendrain.credentials import SecretRedactor
from tokendrain.protocol import GUEST_PORT, MAX_FRAME, FramedPeer


class CredentialPayload(BaseModel):
    openai: RuntimeCredentials
    secrets: dict[str, str] = Field(default_factory=dict, repr=False)
    github_token: str | None = Field(default=None, repr=False)

    @field_validator("secrets")
    @classmethod
    def validate_names(cls, values: dict[str, str]) -> dict[str, str]:
        protected = {
            "HOME",
            "PATH",
            "CODEX_HOME",
            "ACCESS_TOKEN",
            "LD_PRELOAD",
            "LD_LIBRARY_PATH",
            "PYTHONPATH",
            "PYTHONHOME",
            "GIT_CONFIG_GLOBAL",
            "GIT_ASKPASS",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        }
        for name in values:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in protected:
                raise ValueError(f"secret name {name!r} is invalid or reserved")
        return values


class GuestDaemon:
    def __init__(
        self, runtime_dir: Path, codex_home: Path, workspace: Path, executable: str = "codex"
    ) -> None:
        self.runtime_dir, self.codex_home, self.workspace = runtime_dir, codex_home, workspace
        self.executable = executable
        self.credentials: CredentialPayload | None = None
        self.process: asyncio.subprocess.Process | None = None
        self.codex: JsonRpcPeer | None = None
        self.host: FramedPeer | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self.failed = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._init_result: JsonObject = {}
        self._active_thread: str | None = None
        self._active_turn: str | None = None
        self.turn_idle = asyncio.Event()
        self.turn_idle.set()
        self._thread_settings: JsonObject = {}
        self.shutdown_requested = asyncio.Event()
        self.redactor = SecretRedactor()

    def _write_private(self, name: str, content: str, executable: bool = False) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.runtime_dir / f".{name}.tmp"
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700 if executable else 0o600
        )
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        temporary.replace(self.runtime_dir / name)

    def _sync_secrets(self) -> None:
        assert self.credentials
        values = [self.credentials.openai.access_token, *self.credentials.secrets.values()]
        if self.credentials.github_token:
            values.append(self.credentials.github_token)
            self._write_private("github-token", self.credentials.github_token)
        else:
            (self.runtime_dir / "github-token").unlink(missing_ok=True)
        self.redactor.replace_values(values)
        self._write_private("secrets.json", json.dumps(self.credentials.secrets))
        self._write_private(
            "git-credential",
            "#!/bin/sh\n"
            'if [ "$1" = get ]; then\n'
            "  protocol= host=\n"
            '  while IFS="=" read -r key value; do\n'
            '    case "$key" in protocol) protocol="$value";; host) host="$value";; esac\n'
            "  done\n"
            '  if [ "$protocol" = https ] && [ "$host" = github.com ]; then\n'
            "    printf 'username=x-access-token\\npassword='\n"
            f"    cat {self.runtime_dir / 'github-token'}\n"
            "    printf '\\n'\n"
            "  fi\nfi\n",
            executable=True,
        )
        self._write_private(
            "gitconfig",
            f"[credential]\n    helper = \n    helper = {self.runtime_dir / 'git-credential'}\n",
        )

    async def _external_request(self, method: str, params: JsonObject) -> JsonObject:
        if method == "account/chatgptAuthTokens/refresh" and self.host:
            async with asyncio.timeout(8):
                result = await self.host.request("openai_refresh_required", params)
            credentials = RuntimeCredentials.model_validate(result)
            if self.credentials:
                self.credentials.openai = credentials
                self._sync_secrets()
            return {
                "accessToken": credentials.access_token,
                "chatgptAccountId": credentials.account_id,
                "chatgptPlanType": credentials.plan_type,
            }
        # Autonomous VM runs never wait for a human. Unexpected interactive
        # methods fail clearly instead of hanging or silently widening policy.
        raise RpcError(-32601, "interactive method unavailable in autonomous mode")

    async def start_codex(self) -> JsonObject:
        if self.codex and not self.codex.closed.is_set():
            return self._init_result
        if self.process is not None:
            await self.stop_codex()
        self._stopping = False
        self.failed.clear()
        if not self.credentials:
            raise RpcError(-32001, "runtime credentials have not been supplied")
        self.codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._sync_secrets()
        env = {
            **os.environ,
            **self.credentials.secrets,
            "CODEX_HOME": str(self.codex_home),
            "GIT_CONFIG_GLOBAL": str(self.runtime_dir / "gitconfig"),
        }
        if self.credentials.github_token:
            env.update(
                GH_TOKEN=self.credentials.github_token, GITHUB_TOKEN=self.credentials.github_token
            )
        arguments = [
            self.executable,
            "app-server",
            "--listen",
            "stdio://",
            "-c",
            'approval_policy="never"',
            "-c",
            'sandbox_mode="danger-full-access"',
            "-c",
            'cli_auth_credentials_store="ephemeral"',
        ]
        if self.credentials.openai.mode == "siwc":
            env["ACCESS_TOKEN"] = self.credentials.openai.access_token
            for setting in [
                'model_provider="openai_chatgpt_plan"',
                'model_providers.openai_chatgpt_plan.name="ChatGPT plan"',
                'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"',
                'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
                'model_providers.openai_chatgpt_plan.wire_api="responses"',
                "model_providers.openai_chatgpt_plan.requires_openai_auth=false",
                "model_providers.openai_chatgpt_plan.supports_websockets=false",
            ]:
                arguments.extend(["-c", setting])
        self.process = await asyncio.create_subprocess_exec(
            *arguments,
            env=env,
            cwd=self.workspace,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_FRAME,
            start_new_session=True,
        )
        assert self.process.stdout and self.process.stdin
        self.codex = JsonRpcPeer(
            self.process.stdout,
            self.process.stdin,
            request_handler=self._external_request,
            timeout=90,
        )
        await self.codex.start()
        self._tasks = [asyncio.create_task(self._supervise_codex(), name="guest-codex-supervisor")]
        try:
            self._init_result = await CodexClient(self.codex).initialize()
            if self.credentials.openai.mode == "chatgpt":
                await self.codex.request(
                    "account/login/start",
                    {
                        "type": "chatgptAuthTokens",
                        "accessToken": self.credentials.openai.access_token,
                        "chatgptAccountId": self.credentials.openai.account_id,
                        "chatgptPlanType": self.credentials.openai.plan_type,
                    },
                )
            return self._init_result
        except BaseException:
            await self.stop_codex()
            raise

    async def _supervise_codex(self) -> None:
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._forward_notifications(), name="guest-codex-events")
                group.create_task(self._forward_stderr(), name="guest-codex-stderr")
                group.create_task(self._watch_process(), name="guest-codex-watch")
                group.create_task(self._watch_rpc(), name="guest-codex-rpc-watch")
        except* Exception:
            if not self._stopping:
                self.failed.set()
                if self.host:
                    with contextlib.suppress(Exception):
                        async with asyncio.timeout(3):
                            await self.host.notify(
                                "guest/failure",
                                {
                                    "message": (
                                        "Codex process or event channel failed; "
                                        "inspect persisted state before resuming."
                                    )
                                },
                            )
                    # Closing the channel makes failure observable even when the
                    # host is not currently consuming notifications.
                    self.host.writer.close()

    async def _watch_rpc(self) -> None:
        assert self.codex
        await self.codex.closed.wait()
        if not self._stopping:
            raise ConnectionError("Codex RPC disconnected")

    def _redact_event(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redactor.redact(value)
        if isinstance(value, list):
            return [self._redact_event(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact_event(item) for key, item in value.items()}
        return value

    async def _forward_notifications(self) -> None:
        assert self.codex
        while True:
            event = await self.codex.notifications.get()
            if event.method == "turn/completed":
                self._active_turn = None
                self.turn_idle.set()
            if self.host:
                # Redaction is best effort: arbitrary guest software can transform secrets.
                params = self._redact_event(event.params)
                await self.host.notify(event.method, params)

    async def _forward_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            if self.host:
                await self.host.notify(
                    "guest/log", {"message": self.redactor.redact(line.decode(errors="replace"))}
                )

    async def _watch_process(self) -> None:
        assert self.process
        code = await self.process.wait()
        if not self._stopping:
            if self.host:
                await self.host.notify("guest/codex_exited", {"exit_code": code})
            raise RuntimeError("Codex process exited unexpectedly")

    async def stop_codex(self) -> None:
        self._stopping = True
        if self.codex:
            await self.codex.close()
            self.codex = None
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()
        self.process = None
        self._active_turn = None
        self.turn_idle.set()

    async def _restart_at_boundary(self) -> JsonObject:
        if self._active_turn:
            raise RpcError(-32002, "credential rotation requires a completed turn boundary")
        thread = self._active_thread
        await self.stop_codex()
        await self.start_codex()
        if thread:
            assert self.codex
            await self.codex.request("thread/resume", {**self._thread_settings, "threadId": thread})
        return {"thread_id": thread, "restarted": True}

    async def handle(self, method: str, params: JsonObject) -> JsonObject:
        if method == "ping":
            return {"version": 1, "ready": True}
        if method in {"guest_info", "codex_status"}:
            return {
                "version": 1,
                "codex_running": self.process is not None and self.process.returncode is None,
                "thread_id": self._active_thread,
                "turn_id": self._active_turn,
                "workspace": str(self.workspace),
                "codex_home": str(self.codex_home),
                "dropped_output_events": self.codex.dropped_output_events if self.codex else 0,
            }
        if method == "credentials_set":
            if self.process:
                raise RpcError(-32002, "use credential rotation while Codex is running")
            self.credentials = CredentialPayload.model_validate(params)
            self._sync_secrets()
            return {}
        if method == "codex_rpc_notification":
            if self.codex and params["method"] != "initialized":
                await self.codex.notify(params["method"], params.get("params"))
            return {}
        if method == "codex_rpc_request":
            if not self.codex:
                raise RpcError(-32001, "Codex is not running")
            rpc_method, rpc_params = params["method"], params.get("params") or {}
            if rpc_method == "initialize":
                return self._init_result
            if rpc_method == "turn/start":
                if self._active_turn:
                    raise RpcError(-32002, "a turn is already active")
                self._active_turn = "pending"
                self.turn_idle.clear()
            try:
                result = await self.codex.request(rpc_method, rpc_params)
            except BaseException:
                if rpc_method == "turn/start":
                    self._active_turn = None
                    self.turn_idle.set()
                raise
            if rpc_method in {"thread/start", "thread/resume"}:
                self._active_thread = result["thread"]["id"]
                self._thread_settings = {
                    key: value
                    for key, value in rpc_params.items()
                    if key in {"cwd", "model", "approvalPolicy", "sandbox"}
                }
            if rpc_method == "turn/start" and self._active_turn == "pending":
                self._active_turn = result["turn"]["id"]
            return result
        async with self._lifecycle_lock:
            if method == "codex_start":
                return await self.start_codex()
            if method == "codex_stop":
                await self.stop_codex()
                return {}
            if method == "openai_token_rotate":
                if self._active_turn:
                    raise RpcError(-32002, "rotate at a completed turn boundary")
                if not self.credentials:
                    raise RpcError(-32001, "no credentials")
                self.credentials.openai = RuntimeCredentials.model_validate(
                    params.get("openai", params)
                )
                return await self._restart_at_boundary()
            if method == "github_token_rotate":
                if not self.credentials:
                    raise RpcError(-32001, "no credentials")
                if self._active_turn:
                    raise RpcError(-32002, "rotate at a completed turn boundary")
                self.credentials.github_token = str(params["token"])
                self._sync_secrets()
                return await self._restart_at_boundary()
            if method in {"credentials_clear", "shutdown"}:
                await self.stop_codex()
                self.credentials = None
                for path in self.runtime_dir.glob("*"):
                    if path.is_file():
                        path.unlink()
                self.redactor.replace_values([])
                if method == "shutdown":
                    self.shutdown_requested.set()
                return {}
        raise RpcError(-32601, "unknown guest operation")

    async def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.host is not None:
            writer.close()
            await writer.wait_closed()
            return
        peer = FramedPeer(reader, writer, request_handler=self.handle, timeout=90)
        self.host = peer
        await peer.start()
        try:
            await peer.closed.wait()
        finally:
            await self.stop_codex()
            await peer.close()
            self.host = None
            self.credentials = None
            for path in self.runtime_dir.glob("*"):
                if path.is_file():
                    path.unlink()


async def serve(args: argparse.Namespace) -> None:
    daemon = GuestDaemon(
        Path(args.runtime_dir), Path(args.codex_home), Path(args.workspace), args.codex
    )
    connections: set[asyncio.Task[Any]] = set()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(daemon.accept(reader, writer))
        connections.add(task)

        def completed(done: asyncio.Task[Any]) -> None:
            connections.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logging.getLogger(__name__).error("guest control connection failed")

        task.add_done_callback(completed)

    if args.unix_socket:
        socket_path = Path(args.unix_socket)
        await asyncio.to_thread(socket_path.unlink, missing_ok=True)
        server = await asyncio.start_unix_server(accept, path=socket_path, limit=MAX_FRAME)
        await asyncio.to_thread(socket_path.chmod, 0o600)
    else:
        sock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
        sock.bind((socket.VMADDR_CID_ANY, args.port))
        sock.listen(1)
        sock.setblocking(False)
        server = await asyncio.start_server(accept, sock=sock, limit=MAX_FRAME)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, daemon.shutdown_requested.set)
    try:
        await daemon.shutdown_requested.wait()
        if daemon.host:
            # The shutdown method wakes this owner before its RPC reply is
            # serialized; drain accepted requests before closing transport.
            with contextlib.suppress(Exception):
                async with asyncio.timeout(5):
                    await daemon.host.drain_requests()
    finally:
        # Python 3.12 Server.wait_closed also waits for accepted transports.
        # Close the listener first, then clients, before awaiting server closure.
        server.close()
        if daemon.host:
            await daemon.host.close()
        active_connections = tuple(connections)
        try:
            async with asyncio.timeout(20):
                await asyncio.gather(*active_connections, return_exceptions=True)
        except TimeoutError:
            for task in active_connections:
                task.cancel()
            await asyncio.gather(*active_connections, return_exceptions=True)
        await daemon.stop_codex()
        async with asyncio.timeout(5):
            await server.wait_closed()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        if args.poweroff_on_shutdown:
            # Firecracker x86 has no ACPI poweroff; reboot=k makes a graceful
            # Linux reboot terminate the VMM. ARM also exits on guest reboot.
            process = await asyncio.create_subprocess_exec(
                "systemctl",
                "--no-block",
                "reboot",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(process.communicate(), 10)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise
            if process.returncode:
                raise RuntimeError(
                    "guest systemd shutdown request failed: "
                    + stderr.decode(errors="replace")[:1000]
                )


def main() -> None:
    parser = argparse.ArgumentParser(prog="tokendrain-guestd")
    parser.add_argument("--port", type=int, default=GUEST_PORT)
    parser.add_argument("--runtime-dir", default="/run/tokendrain")
    parser.add_argument("--codex-home", default="/persist/codex")
    parser.add_argument("--workspace", default="/workspace")
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--poweroff-on-shutdown", action="store_true")
    parser.add_argument("--unix-socket", help="local protocol testing only")
    asyncio.run(serve(parser.parse_args()))


if __name__ == "__main__":
    main()
