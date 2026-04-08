"""Accessibility interface — query the platform accessibility tree."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from cua_sandbox.transport.base import Transport


class Accessibility:
    """Accessibility tree queries backed by computer-server's ``get_accessibility_tree`` command.

    Usage::

        tree = await sb.accessibility.get_tree()
        node = await sb.accessibility.find(role="button", title="Submit")
        texts = await sb.accessibility.all_text()
        assert "The proxy is working correctly." in texts
    """

    def __init__(self, transport: Transport):
        self._t = transport

    async def get_tree(self) -> Dict[str, Any]:
        """Return the full accessibility tree of the current focused window.

        The returned dict structure is OS-dependent but always contains at
        minimum ``{"role": str, "title": str, "children": [...]}`` at the root.
        """
        result = await self._t.send("get_accessibility_tree")
        if isinstance(result, dict):
            return result
        return {"raw": result}

    async def find(
        self,
        *,
        role: Optional[str] = None,
        title: Optional[str] = None,
        value: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Find the first accessibility node matching any of the given criteria.

        Args:
            role:  AT role string, e.g. ``"button"``, ``"staticText"``, ``"webArea"``.
            title: Accessible name / label.
            value: Current value of an input or text node.

        Returns the matching node dict, or ``None`` if not found.
        """
        params: Dict[str, Any] = {}
        if role is not None:
            params["role"] = role
        if title is not None:
            params["title"] = title
        if value is not None:
            params["value"] = value
        result = await self._t.send("find_element", **params)
        if isinstance(result, dict) and result.get("found"):
            return result.get("element")
        return None

    async def all_text(self) -> List[str]:
        """Return every visible text string in the current window's a11y tree.

        Useful for asserting that a particular string is (or is not) rendered
        on screen without doing pixel-level OCR::

            texts = await sb.accessibility.all_text()
            assert "The proxy is working correctly." in texts
        """
        tree = await self.get_tree()
        texts: List[str] = []
        _collect_text(tree, texts)
        return texts

    async def contains_text(self, text: str) -> bool:
        """Return True if *text* appears anywhere in the a11y tree."""
        return text in await self.all_text()


def _collect_text(node: Any, out: List[str]) -> None:
    """Recursively harvest text / title / value strings from an a11y node."""
    if not isinstance(node, dict):
        return
    for key in ("title", "value", "name", "description", "AXValue", "AXTitle", "AXDescription"):
        v = node.get(key)
        if isinstance(v, str) and v.strip():
            out.append(v.strip())
    for child in node.get("children", []):
        _collect_text(child, out)
    # Some transports return a flat list under "nodes"
    for child in node.get("nodes", []):
        _collect_text(child, out)
