"""HTTP-backed implementation of OpenCollab's environment contract."""

from __future__ import annotations

import asyncio
import contextlib
import math
import uuid
from typing import Any

import httpx

from opencollab.adapters._env_base import ExecResult, TextFileRange


class RemoteEnvironment:
    """Run OpenCollab tools through an Execution Server."""

    local_filesystem = False
    process_isolated = True

    def __init__(
        self,
        base_url: str,
        *,
        image: str,
        request_timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be non-empty text")
        if not isinstance(image, str) or not image.strip():
            raise ValueError("image must be non-empty text")
        if (
            isinstance(request_timeout, bool)
            or not isinstance(request_timeout, (int, float))
            or not math.isfinite(request_timeout)
            or request_timeout <= 0
        ):
            raise ValueError("request_timeout must be a positive finite number")
        self._root = base_url.rstrip("/") + "/v1"
        self._image = image
        self._http = httpx.AsyncClient(
            timeout=float(request_timeout),
            trust_env=False,
            transport=transport,
        )
        self._id: str | None = None
        self._generation = 0
        self._revoked = False
        self.workspace = "."
        self.host_workspace: str | None = None
        self.source_workspace: str | None = None

    @property
    def revoked(self) -> bool:
        return self._revoked

    def revoke(self) -> None:
        self._revoked = True

    async def setup(self, mount_dir: str | None = None) -> str:
        if mount_dir is not None:
            raise ValueError("mount_dir is not supported by a remote environment")
        if self._id is not None:
            return self.workspace
        data = await self._request("POST", "/environments", json={"image": self._image})
        self._id = data["id"]
        self._generation = data.get("generation", 0)
        self.workspace = data["workspace"]
        return self.workspace

    async def abort(self) -> None:
        self.revoke()
        if self._id is not None:
            with contextlib.suppress(Exception):
                await self._http.post(f"{self._root}/environments/{self._id}/abort")

    async def cleanup(self) -> None:
        if self._id is not None:
            with contextlib.suppress(Exception):
                await self._http.delete(f"{self._root}/environments/{self._id}")
            self._id = None
        with contextlib.suppress(Exception):
            await self._http.aclose()

    async def heartbeat(self) -> None:
        self._ensure_active()
        await self._request("POST", f"/environments/{self._id}/heartbeat")

    async def exec_cmd(self, cmd: str, timeout: float = 120.0) -> ExecResult:
        self._ensure_active()
        execution_id = "exec_" + uuid.uuid4().hex[:12]
        try:
            await self._request(
                "POST",
                f"/environments/{self._id}/executions",
                json={
                    "command": cmd,
                    "timeout": timeout,
                    "execution_id": execution_id,
                },
            )
            completed = await self._request("GET", f"/executions/{execution_id}")
        except asyncio.CancelledError:
            await asyncio.shield(self._confirm_cancelled(execution_id))
            raise
        if completed.get("state") == "cancelled":
            raise asyncio.CancelledError
        return ExecResult(**completed["result"])

    async def _confirm_cancelled(self, execution_id: str, attempts: int = 5) -> None:
        last_error = ""
        for _ in range(attempts):
            try:
                response = await self._http.delete(
                    f"{self._root}/executions/{execution_id}",
                    headers=self._generation_headers(),
                )
            except Exception as exc:  # noqa: BLE001
                last_error = repr(exc)
                await asyncio.sleep(0.2)
                continue
            if response.status_code == 404:
                # A cancelled submission and its DELETE can cross in transit.
                # "Not registered yet" is not proof that the server will never
                # admit the already-sent POST, so retry and ultimately fail
                # closed instead of reporting false quiescence.
                last_error = f"HTTP 404: {response.text}"
                await asyncio.sleep(0.2)
                continue
            payload = response.json() if response.status_code < 400 else {}
            if response.status_code < 400 and payload.get("quiesced") is True:
                if payload.get("sandbox") == "torn_down":
                    self.revoke()
                return
            last_error = f"HTTP {response.status_code}: {response.text}"
            await asyncio.sleep(0.2)
        raise RuntimeError(
            f"could not confirm remote quiescence for {execution_id}: {last_error}"
        )

    async def read_file(self, path: str) -> str:
        self._ensure_active()
        data = await self._request(
            "POST", f"/environments/{self._id}/files/read", json={"path": path}
        )
        return data["content"]

    async def read_text_range(
        self,
        path: str,
        *,
        offset: int,
        limit: int,
        max_chars: int,
    ) -> TextFileRange:
        self._ensure_active()
        data = await self._request(
            "POST",
            f"/environments/{self._id}/files/read_range",
            json={
                "path": path,
                "offset": offset,
                "limit": limit,
                "max_chars": max_chars,
            },
        )
        return TextFileRange(**data)

    async def write_file(self, path: str, content: str) -> None:
        self._ensure_active()
        await self._request(
            "POST",
            f"/environments/{self._id}/files/write",
            json={"path": path, "content": content},
        )

    async def write_temp_file(
        self,
        content: str,
        *,
        prefix: str,
        suffix: str = ".tmp",
    ) -> str:
        self._ensure_active()
        data = await self._request(
            "POST",
            f"/environments/{self._id}/files/write_temp",
            json={"content": content, "prefix": prefix, "suffix": suffix},
        )
        return data["path"]

    async def remove_file(self, path: str) -> None:
        self._ensure_active()
        await self._request(
            "POST", f"/environments/{self._id}/files/remove", json={"path": path}
        )

    async def checkpoint(self, label: str | None = None) -> str:
        self._ensure_active()
        data = await self._request(
            "POST",
            f"/environments/{self._id}/checkpoints",
            json={"label": label},
        )
        return data["checkpoint_id"]

    async def restore(self, checkpoint_id: str) -> None:
        self._ensure_active()
        data = await self._request(
            "POST",
            f"/environments/{self._id}/checkpoints/{checkpoint_id}/restore",
        )
        self._generation = data.get("generation", self._generation + 1)

    async def discard(self, checkpoint_id: str) -> None:
        self._ensure_active()
        await self._request(
            "DELETE", f"/environments/{self._id}/checkpoints/{checkpoint_id}"
        )

    def _ensure_active(self) -> None:
        if self._revoked:
            raise RuntimeError("remote environment has been revoked")
        if self._id is None:
            raise RuntimeError("call setup() before using the remote environment")

    def _generation_headers(self) -> dict[str, str]:
        if self._id is None:
            return {}
        return {"X-Sandbox-Generation": str(self._generation)}

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(self._generation_headers())
        response = await self._http.request(
            method,
            self._root + path,
            headers=headers,
            **kwargs,
        )
        if response.status_code >= 400:
            self._raise_for(response)
        return response.json()

    def _raise_for(self, response: httpx.Response) -> None:
        try:
            detail = response.json().get("detail", {})
            code = detail.get("code", "")
            message = detail.get("message", response.text)
        except Exception:  # noqa: BLE001
            code, message = "", response.text
        if code == "sandbox_torn_down":
            self.revoke()
        mapping: dict[str, type[Exception]] = {
            "path_escapes_workspace": PermissionError,
            "not_found": FileNotFoundError,
            "invalid_argument": ValueError,
            "os_error": OSError,
        }
        raise mapping.get(code, RuntimeError)(message or f"HTTP {response.status_code}")


__all__ = ["RemoteEnvironment"]
