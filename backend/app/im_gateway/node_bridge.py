from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from app.config import settings
from app.network_environment import proxy_free_environment


class WeComBridgeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


BridgeEventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class WeComNodeBridge:
    """Async JSON-lines client for the co-located official Node SDK bridge."""

    def __init__(
        self,
        *,
        node_binary: str | None = None,
        script_path: Path | None = None,
        event_handler: BridgeEventHandler | None = None,
    ) -> None:
        self.node_binary = node_binary or settings.IM_GATEWAY_NODE_BINARY
        self.script_path = script_path or (
            Path(__file__).with_name("node_bridge") / "bridge.mjs"
        )
        self.event_handler = event_handler
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._temp_directory: TemporaryDirectory[str] | None = None
        self.temp_root: Path | None = None

    @property
    def is_running(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    @staticmethod
    def _process_environment(temp_root: Path) -> dict[str, str]:
        # WeCom's WebSocket server (openws.work.weixin.qq.com) rejects TLS 1.3
        # connections from Node.js 22+ (OpenSSL 3.5+), causing ECONNRESET.
        # Cap TLS at 1.2 for the bridge subprocess until WeCom fixes this.
        node_opts = os.environ.get("NODE_OPTIONS", "")
        if "--tls-max-v" not in node_opts:
            node_opts = f"{node_opts} --tls-max-v1.2".strip()
        return proxy_free_environment(
            overrides={
                "IM_BRIDGE_TEMP_ROOT": str(temp_root),
                "NODE_OPTIONS": node_opts,
            }
        )

    async def start(self) -> dict[str, Any]:
        if self.is_running:
            return await self.request("ping")
        if (
            self._process is not None
            or self._reader_task is not None
            or self._stderr_task is not None
            or self._temp_directory is not None
        ):
            await self.stop()
        self._temp_directory = TemporaryDirectory(
            prefix="hermes-im-gateway-"
        )
        self.temp_root = Path(self._temp_directory.name).resolve()
        os.chmod(self.temp_root, 0o700)
        environment = self._process_environment(self.temp_root)
        self._process = await asyncio.create_subprocess_exec(
            self.node_binary,
            str(self.script_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        self._reader_task = asyncio.create_task(
            self._read_stdout(), name="wecom-node-bridge-stdout"
        )
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="wecom-node-bridge-stderr"
        )
        try:
            return await self.request("ping", timeout=15)
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.returncode is None:
            try:
                await self.request(
                    "shutdown", process=process, timeout=3
                )
            except Exception:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            if not await self._wait_for_exit(process, timeout=5):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await self._wait_for_exit(process, timeout=2)
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._reader_task = None
        self._stderr_task = None
        self._fail_pending(
            WeComBridgeError("BRIDGE_STOPPED", "WeCom bridge stopped")
        )
        if self._temp_directory is not None:
            self._temp_directory.cleanup()
            self._temp_directory = None
        self.temp_root = None

    async def request(
        self,
        operation: str,
        *,
        timeout: float = 30,
        process: asyncio.subprocess.Process | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        target = process or self._process
        if (
            target is None
            or target.returncode is not None
            or target.stdin is None
        ):
            raise WeComBridgeError(
                "BRIDGE_NOT_RUNNING", "WeCom bridge is not running"
            )
        command_id = uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[command_id] = future
        command = {
            "id": command_id,
            "op": operation,
            **payload,
        }
        encoded = (
            json.dumps(
                command,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        try:
            async with self._write_lock:
                target.stdin.write(encoded)
                await target.stdin.drain()
            return await asyncio.wait_for(future, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise WeComBridgeError(
                "BRIDGE_TIMEOUT",
                f"WeCom bridge operation timed out: {operation}",
            ) from exc
        finally:
            self._pending.pop(command_id, None)

    async def connect(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        bot_id: str,
        secret: str,
        ws_url: str | None = None,
    ) -> None:
        await self.request(
            "connect",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            bot_id=bot_id,
            secret=secret,
            ws_url=ws_url,
        )

    async def disconnect(self, channel_id: int) -> None:
        await self.request("disconnect", channel_id=channel_id)

    async def reply_stream(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        provider_request_id: str,
        stream_id: str,
        content: str,
        finish: bool,
        non_blocking: bool = True,
        msg_item: list[dict[str, Any]] | None = None,
        feedback: dict[str, Any] | None = None,
    ) -> bool:
        result = await self.request(
            "reply_stream",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            provider_request_id=provider_request_id,
            stream_id=stream_id,
            content=content,
            finish=finish,
            non_blocking=non_blocking,
            msg_item=msg_item,
            feedback=feedback,
            timeout=60,
        )
        return bool(result.get("delivered"))

    async def submit_stream_state(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        provider_request_id: str,
        stream_id: str,
        content: str,
    ) -> None:
        result = await self.request(
            "submit_stream_state",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            provider_request_id=provider_request_id,
            stream_id=stream_id,
            content=content,
            timeout=5,
        )
        if result.get("submitted") is not True:
            raise WeComBridgeError(
                "BRIDGE_INVALID_RESPONSE",
                "WeCom bridge did not confirm local stream submission",
            )

    async def reply(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        provider_request_id: str,
        body: dict[str, Any],
        command: str | None = None,
    ) -> None:
        await self.request(
            "reply",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            provider_request_id=provider_request_id,
            body=body,
            cmd=command,
            timeout=60,
        )

    async def reply_stream_with_card(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        provider_request_id: str,
        stream_id: str,
        content: str,
        finish: bool,
        options: dict[str, Any] | None = None,
    ) -> None:
        await self.request(
            "reply_stream_with_card",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            provider_request_id=provider_request_id,
            stream_id=stream_id,
            content=content,
            finish=finish,
            options=options,
            timeout=60,
        )

    async def reply_welcome(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        provider_request_id: str,
        body: dict[str, Any],
    ) -> None:
        await self.request(
            "reply_welcome",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            provider_request_id=provider_request_id,
            body=body,
            timeout=60,
        )

    async def reply_template_card(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        provider_request_id: str,
        template_card: dict[str, Any],
        feedback: dict[str, Any] | None = None,
    ) -> None:
        await self.request(
            "reply_template_card",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            provider_request_id=provider_request_id,
            template_card=template_card,
            feedback=feedback,
            timeout=60,
        )

    async def update_template_card(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        provider_request_id: str,
        template_card: dict[str, Any],
        userids: list[str] | None = None,
    ) -> None:
        await self.request(
            "update_template_card",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            provider_request_id=provider_request_id,
            template_card=template_card,
            userids=userids,
            timeout=60,
        )

    async def send_message(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        chat_id: str,
        body: dict[str, Any],
    ) -> None:
        await self.request(
            "send_message",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            chat_id=chat_id,
            body=body,
            timeout=60,
        )

    async def send_markdown(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        chat_id: str,
        content: str,
    ) -> None:
        await self.request(
            "send_markdown",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            chat_id=chat_id,
            content=content,
            timeout=60,
        )

    async def download_media(
        self,
        *,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        url: str,
        aes_key: str | None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        result = await self.request(
            "download_media",
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            url=url,
            aes_key=aes_key,
            filename=filename,
            timeout=120,
        )
        result_path = self.require_scoped_path(str(result.get("path") or ""))
        return {**result, "path": str(result_path)}

    async def upload_media(
        self,
        *,
        passive: bool,
        channel_id: int,
        generation: int,
        lease_epoch: int,
        media_type: str,
        source_path: Path,
        filename: str,
        provider_request_id: str | None = None,
        chat_id: str | None = None,
        video_options: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        scoped_path = self.require_scoped_path(str(source_path))
        operation = (
            "upload_reply_media" if passive else "upload_send_media"
        )
        return await self.request(
            operation,
            channel_id=channel_id,
            generation=generation,
            lease_epoch=lease_epoch,
            media_type=media_type,
            path=str(scoped_path),
            filename=filename,
            provider_request_id=provider_request_id,
            chat_id=chat_id,
            video_options=video_options,
            timeout=300,
        )

    def require_scoped_path(self, candidate: str) -> Path:
        if self.temp_root is None:
            raise WeComBridgeError(
                "BRIDGE_NOT_RUNNING", "bridge temp root is unavailable"
            )
        resolved = Path(candidate).resolve(strict=False)
        if resolved == self.temp_root or not resolved.is_relative_to(
            self.temp_root
        ):
            raise WeComBridgeError(
                "INVALID_MEDIA_PATH", "media path escapes bridge temp root"
            )
        return resolved

    async def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                try:
                    payload = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "response":
                    command_id = payload.get("id")
                    future = self._pending.get(str(command_id))
                    if future is None or future.done():
                        continue
                    if payload.get("ok") is True:
                        result = payload.get("result")
                        future.set_result(
                            result if isinstance(result, dict) else {}
                        )
                    else:
                        error = payload.get("error")
                        if not isinstance(error, dict):
                            error = {}
                        future.set_exception(
                            WeComBridgeError(
                                str(
                                    error.get("code")
                                    or "WECOM_BRIDGE_ERROR"
                                ),
                                str(
                                    error.get("message")
                                    or "WeCom bridge operation failed"
                                ),
                            )
                        )
                elif self.event_handler is not None:
                    asyncio.create_task(self.event_handler(payload))
        finally:
            self._fail_pending(
                WeComBridgeError(
                    "BRIDGE_EXITED", "WeCom bridge process exited"
                )
            )

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        # The bridge itself emits no business logs. Discard stderr to make
        # accidental SDK diagnostics incapable of entering Backend logs.
        while await process.stderr.readline():
            pass

    def _fail_pending(self, error: WeComBridgeError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    @staticmethod
    async def _wait_for_exit(
        process: asyncio.subprocess.Process, *, timeout: float
    ) -> bool:
        # Some asyncio child-watcher implementations can miss waking a
        # Process.wait() waiter even though returncode is already observable.
        # Polling that read-only state keeps Backend shutdown bounded.
        deadline = asyncio.get_running_loop().time() + timeout
        while (
            process.returncode is None
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.05)
        return process.returncode is not None
