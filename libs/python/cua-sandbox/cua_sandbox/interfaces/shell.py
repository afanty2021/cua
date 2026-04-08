"""Shell interface — run commands, backed by a Transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from cua_sandbox.transport.base import Transport


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    returncode: int

    @property
    def success(self) -> bool:
        return self.returncode == 0


class Shell:
    """Shell command execution."""

    def __init__(self, transport: Transport):
        self._t = transport

    async def run(self, command: str, timeout: int = 30, *, user: Optional[str] = None) -> CommandResult:
        """Run a shell command and return the result.

        Args:
            command: Shell command to execute.
            timeout: Seconds before the command is killed.
            user:    Run as this user (e.g. ``"root"``).  Transport-dependent:
                     ADB wraps with ``su 0 -c …``; SSH/HTTP wrap with
                     ``sudo -u <user> sh -c …``.
        """
        result = await self._t.send("run_command", command=command, timeout=timeout, user=user)
        if isinstance(result, dict):
            rc = result.get("returncode", result.get("return_code", -1))
            return CommandResult(
                stdout=result.get("stdout", ""),
                stderr=result.get("stderr", ""),
                returncode=rc if rc is not None else 0,
            )
        # LocalTransport returns cua_auto.shell.CommandResult directly
        return CommandResult(
            stdout=getattr(result, "stdout", ""),
            stderr=getattr(result, "stderr", ""),
            returncode=getattr(result, "returncode", -1),
        )
