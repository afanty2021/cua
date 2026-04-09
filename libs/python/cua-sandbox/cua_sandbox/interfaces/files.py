"""Files interface — upload, download, read, write, list files in a sandbox.

Usage::

    # Upload a local file
    await sb.files.upload("/local/script.py", "/tmp/script.py")

    # Upload raw bytes / string content
    await sb.files.write("/tmp/config.json", b'{"key": "value"}')
    await sb.files.write("/tmp/hello.txt", "hello world")

    # Download a file to host
    content: bytes = await sb.files.read("/tmp/output.bin")
    await sb.files.download("/tmp/result.csv", "/local/result.csv")

    # List directory
    entries = await sb.files.ls("/tmp")
    print(entries)  # ["/tmp/foo.txt", "/tmp/bar/"]

    # Check existence / stat
    exists = await sb.files.exists("/tmp/foo.txt")
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import List, Union

from cua_sandbox.transport.base import Transport


class Files:
    """File system interface — transfer and inspect files inside a sandbox."""

    def __init__(self, transport: Transport) -> None:
        self._t = transport

    # ── write / upload ────────────────────────────────────────────────────────

    async def write(
        self,
        remote: str,
        content: Union[bytes, str],
        *,
        timeout: int = 60,
    ) -> None:
        """Write *content* (bytes or str) to *remote* path inside the sandbox.

        Uses the transport's ``write_bytes`` action (supported by
        computer-server on all platforms) with a base64-encoded body.
        Falls back to a ``cat | base64 -d`` shell pipe for transports that
        don't expose ``write_bytes`` directly.
        """
        if isinstance(content, str):
            content = content.encode()
        content_b64 = base64.b64encode(content).decode()
        try:
            await self._t.send("write_bytes", path=remote,
                               content_b64=content_b64, timeout=timeout)
        except (NotImplementedError, KeyError, Exception):
            # Fallback: pipe through shell
            await self._t.send(
                "run_command",
                command=f"printf '%s' '{content_b64}' | base64 -d > {remote}",
                timeout=timeout,
                user=None,
            )

    async def upload(
        self,
        local: Union[str, Path],
        remote: str,
        *,
        timeout: int = 60,
    ) -> None:
        """Upload a local file to *remote* path inside the sandbox."""
        content = Path(local).read_bytes()
        await self.write(remote, content, timeout=timeout)

    # ── read / download ───────────────────────────────────────────────────────

    async def read(self, remote: str, *, timeout: int = 60) -> bytes:
        """Return the raw bytes of *remote* from inside the sandbox.

        Reads via ``read_bytes`` transport action, falling back to
        ``base64 <file>`` over a shell command.
        """
        try:
            result = await self._t.send("read_bytes", path=remote, timeout=timeout)
            b64 = result.get("content_b64") or result.get("result", "")
            return base64.b64decode(b64)
        except (NotImplementedError, KeyError, Exception):
            # Fallback: base64-encode on the remote side, decode here
            result = await self._t.send(
                "run_command",
                command=f"base64 {remote}",
                timeout=timeout,
                user=None,
            )
            b64 = result.get("stdout", "").strip()
            return base64.b64decode(b64) if b64 else b""

    async def read_text(self, remote: str, encoding: str = "utf-8", *,
                        timeout: int = 60) -> str:
        """Return the text content of *remote* from inside the sandbox."""
        return (await self.read(remote, timeout=timeout)).decode(encoding)

    async def download(
        self,
        remote: str,
        local: Union[str, Path],
        *,
        timeout: int = 60,
    ) -> None:
        """Download *remote* from the sandbox to a *local* path."""
        content = await self.read(remote, timeout=timeout)
        Path(local).write_bytes(content)

    # ── listing / existence ───────────────────────────────────────────────────

    async def ls(self, remote_dir: str = ".", *, timeout: int = 30) -> List[str]:
        """List files and directories inside *remote_dir*.

        Returns absolute paths.  Directories are suffixed with ``/``.
        """
        result = await self._t.send(
            "run_command",
            command=(
                f"python3 -c \""
                f"import os,json; d='{remote_dir}'; "
                f"entries=[os.path.join(d,e)+('/' if os.path.isdir(os.path.join(d,e)) else '') "
                f"for e in sorted(os.listdir(d))]; print(json.dumps(entries))\""
            ),
            timeout=timeout,
            user=None,
        )
        import json
        try:
            return json.loads(result.get("stdout", "[]").strip())
        except Exception:
            return []

    async def exists(self, remote: str, *, timeout: int = 15) -> bool:
        """Return True if *remote* exists inside the sandbox."""
        result = await self._t.send(
            "run_command",
            command=f"test -e {remote} && echo YES || echo NO",
            timeout=timeout,
            user=None,
        )
        return "YES" in result.get("stdout", "")

    async def stat(self, remote: str, *, timeout: int = 15) -> dict:
        """Return basic stat info for *remote* (size, mtime, type)."""
        import json
        result = await self._t.send(
            "run_command",
            command=(
                f"python3 -c \""
                f"import os,json,stat; s=os.stat('{remote}'); "
                f"print(json.dumps({{'size':s.st_size,'mtime':s.st_mtime,"
                f"'is_dir':stat.S_ISDIR(s.st_mode),'is_file':stat.S_ISREG(s.st_mode)}}))\""
            ),
            timeout=timeout,
            user=None,
        )
        try:
            return json.loads(result.get("stdout", "{}").strip())
        except Exception:
            return {}

    async def mkdir(self, remote: str, *, parents: bool = True,
                    timeout: int = 15) -> None:
        """Create directory *remote* inside the sandbox."""
        flag = "-p" if parents else ""
        await self._t.send(
            "run_command",
            command=f"mkdir {flag} {remote}",
            timeout=timeout,
            user=None,
        )

    async def rm(self, remote: str, *, recursive: bool = False,
                 timeout: int = 15) -> None:
        """Remove *remote* inside the sandbox."""
        flag = "-rf" if recursive else "-f"
        await self._t.send(
            "run_command",
            command=f"rm {flag} {remote}",
            timeout=timeout,
            user=None,
        )
