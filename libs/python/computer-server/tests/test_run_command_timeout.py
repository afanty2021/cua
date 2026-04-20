"""Tests for SDK-configurable ``run_command`` timeout propagation.

The SDK's ``cua_sandbox.interfaces.shell.Shell.run(cmd, timeout=...)`` sends
the timeout as a ``/cmd`` param; ``computer_server.main``'s dispatcher
filters kwargs by the handler signature before forwarding.  These tests
cover the handler-side contract:

* handler accepts ``timeout`` as a float keyword
* ``timeout=None`` → waits indefinitely
* ``timeout=<float>`` → times out at expiry with a ``success=False`` result
  containing the standardised ``Command timed out after <t>s`` stderr and
  ``return_code=-1``

The Android / Windows variants share the same shape; the base handler is
exercised here because it exposes a POSIX subprocess without adb/emulator
state being required for a focused unit test.
"""

import asyncio

import pytest

from computer_server.handlers.base import BaseAutomationHandler


class _MinimalHandler(BaseAutomationHandler):
    """Concrete BaseAutomationHandler with the abstract methods stubbed out."""

    async def mouse_down(self, x=None, y=None, button="left"):  # pragma: no cover
        return {}

    async def mouse_up(self, x=None, y=None, button="left"):  # pragma: no cover
        return {}

    async def left_click(self, x=None, y=None):  # pragma: no cover
        return {}

    async def right_click(self, x=None, y=None):  # pragma: no cover
        return {}

    async def double_click(self, x=None, y=None):  # pragma: no cover
        return {}

    async def move_cursor(self, x, y):  # pragma: no cover
        return {}

    async def drag_to(self, x, y, button="left", duration=0.5):  # pragma: no cover
        return {}

    async def drag(self, path, button="left", duration=0.5):  # pragma: no cover
        return {}

    async def key_down(self, key):  # pragma: no cover
        return {}

    async def key_up(self, key):  # pragma: no cover
        return {}

    async def type_text(self, text):  # pragma: no cover
        return {}

    async def press_key(self, key):  # pragma: no cover
        return {}

    async def hotkey(self, *keys):  # pragma: no cover
        return {}

    async def scroll(self, x, y):  # pragma: no cover
        return {}

    async def scroll_down(self, clicks=1):  # pragma: no cover
        return {}

    async def scroll_up(self, clicks=1):  # pragma: no cover
        return {}

    async def get_cursor_position(self):  # pragma: no cover
        return {}

    async def get_screen_size(self):  # pragma: no cover
        return {}

    async def screenshot(self):  # pragma: no cover
        return {}

    async def copy_to_clipboard(self):  # pragma: no cover
        return {}

    async def set_clipboard(self, text):  # pragma: no cover
        return {}


class TestRunCommandTimeout:
    @pytest.mark.asyncio
    async def test_no_timeout_defaults_to_indefinite_wait(self, monkeypatch):
        # Ensure we hit the subprocess_shell branch (not the android adb branch).
        monkeypatch.delenv("IS_CUA_ANDROID", raising=False)
        h = _MinimalHandler()

        result = await h.run_command("echo hello")
        assert result["success"] is True
        assert result["stdout"].strip() == "hello"
        assert result["return_code"] == 0

    @pytest.mark.asyncio
    async def test_timeout_honoured_when_passed(self, monkeypatch):
        monkeypatch.delenv("IS_CUA_ANDROID", raising=False)
        h = _MinimalHandler()

        # sleep 5, cap at 0.2 — should time out
        start = asyncio.get_event_loop().time()
        result = await h.run_command("sleep 5", timeout=0.2)
        elapsed = asyncio.get_event_loop().time() - start

        assert result["success"] is False
        assert "timed out" in result["stderr"].lower()
        assert result["return_code"] == -1
        # Must have returned within a small factor of the requested timeout.
        assert elapsed < 2.0, f"expected quick timeout, took {elapsed:.2f}s"

    @pytest.mark.asyncio
    async def test_fast_command_under_timeout_succeeds(self, monkeypatch):
        monkeypatch.delenv("IS_CUA_ANDROID", raising=False)
        h = _MinimalHandler()

        result = await h.run_command("echo done", timeout=10.0)
        assert result["success"] is True
        assert result["stdout"].strip() == "done"
