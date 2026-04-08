"""MitmProxy — builder and runtime handle for mitmproxy transparent-proxy sidecars.

Builder (image-time)
--------------------
``MitmProxy`` is a pure builder that returns an ``Image``.  Each classmethod
produces a pre-configured mitmproxy sidecar ready to pass to
``Image.with_proxy()``::

    from cua_sandbox.mitm import MitmProxy

    # capture only
    proxy = MitmProxy.image()

    # rewrite response bodies
    proxy = MitmProxy.replace(
        url_pattern="~u example.com",
        search="This domain is for use in illustrative examples",
        replacement="The proxy is working correctly.",
    )

    # arbitrary mitmproxy addon script
    proxy = MitmProxy.script(\"\"\"
        from mitmproxy import http
        def response(flow):
            flow.response.text = flow.response.text.replace("foo", "bar")
    \"\"\")

Runtime handle (``sb.proxy``)
-----------------------------
``MitmProxyHandle`` is attached to ``sb.proxy`` after the sandbox starts.  It
lets you read captured traffic without touching the container directly::

    flows = await sb.proxy.flows()
    for f in flows:
        print(f.url, f.response_status, len(f.response_body or b""))

    await sb.proxy.export_flows("/tmp/capture.mitm")
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from cua_sandbox.image import Image
    from cua_sandbox.interfaces.shell import Shell

# ── mitmproxy addon templates ────────────────────────────────────────────────

_FLOW_LOG_ADDON = """\
import json, time
from mitmproxy import http

_FLOWS: list[dict] = []
_MAX_BODY = 512 * 1024  # 512 KB — skip binary / large responses

def _body_text(flow):
    if flow.response is None:
        return None
    ct = flow.response.headers.get("content-type", "")
    if not any(t in ct for t in ("text", "json", "xml", "javascript", "html")):
        return None  # skip binary content
    if flow.response.content and len(flow.response.content) > _MAX_BODY:
        return None  # skip very large responses
    return flow.response.get_text(strict=False)

def response(flow: http.HTTPFlow) -> None:
    entry = {
        "url": flow.request.pretty_url,
        "method": flow.request.method,
        "request_headers": dict(flow.request.headers),
        "response_status": flow.response.status_code if flow.response else None,
        "response_headers": dict(flow.response.headers) if flow.response else {},
        "response_body": _body_text(flow),
        "timestamp": time.time(),
    }
    _FLOWS.append(entry)
    with open("/tmp/mitm_flows.json", "w") as fh:
        json.dump(_FLOWS, fh)
"""

_REPLACE_ADDON_TEMPLATE = """\
import re, json, time
from mitmproxy import http, ctx

PATTERN   = {pattern!r}
SEARCH    = {search!r}
REPLACE   = {replace!r}

_FLOWS: list[dict] = []
_MAX_BODY = 512 * 1024  # 512 KB

def _is_text(flow):
    ct = flow.response.headers.get("content-type", "")
    return any(t in ct for t in ("text", "json", "xml", "javascript", "html"))

def response(flow: http.HTTPFlow) -> None:
    if flow.response is None:
        return
    # Only attempt body replacement on text responses
    if re.search(PATTERN, flow.request.pretty_url) and _is_text(flow):
        body = flow.response.get_text(strict=False) or ""
        if SEARCH in body:
            flow.response.text = body.replace(SEARCH, REPLACE)
            ctx.log.info(f"[mitm-replace] rewrote {{flow.request.pretty_url!r}}")
    # Capture body only for small text responses
    body_text = None
    if _is_text(flow) and flow.response.content and len(flow.response.content) <= _MAX_BODY:
        body_text = flow.response.get_text(strict=False)
    _entry = {{
        "url": flow.request.pretty_url,
        "method": flow.request.method,
        "request_headers": dict(flow.request.headers),
        "response_status": flow.response.status_code,
        "response_headers": dict(flow.response.headers),
        "response_body": body_text,
        "timestamp": time.time(),
    }}
    _FLOWS.append(_entry)
    with open("/tmp/mitm_flows.json", "w") as fh:
        json.dump(_FLOWS, fh)
"""

_ENTRYPOINT_SH = """\
#!/bin/sh
set -e
ADDON=${MITM_ADDON_PATH:-/mitm_addon.py}
exec mitmdump \\
    --mode transparent \\
    --listen-port 8080 \\
    --set "confdir=/root/.mitmproxy" \\
    --set ssl_insecure=true \\
    --flow-detail 1 \\
    --save-stream-file /tmp/mitm_flows.bin \\
    -s "$ADDON"
"""


# ── dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class MitmFlow:
    """A single captured HTTP/HTTPS flow."""

    url: str
    method: str
    request_headers: Dict[str, str] = field(default_factory=dict)
    response_status: Optional[int] = None
    response_headers: Dict[str, str] = field(default_factory=dict)
    response_body: Optional[str] = None
    timestamp: float = 0.0

    @property
    def response_body_text(self) -> str:
        return self.response_body or ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MitmFlow":
        return cls(
            url=d.get("url", ""),
            method=d.get("method", "GET"),
            request_headers=d.get("request_headers", {}),
            response_status=d.get("response_status"),
            response_headers=d.get("response_headers", {}),
            response_body=d.get("response_body"),
            timestamp=d.get("timestamp", 0.0),
        )


class MitmProxyHandle:
    """Runtime handle for the proxy sidecar (``sb.proxy``).

    Obtained automatically when a sandbox is started from a topology that
    has a proxy image.  Never construct directly.
    """

    def __init__(self, proxy_shell: "Shell", proxy_container: Optional[str] = None):
        self._shell = proxy_shell
        self._container = proxy_container

    async def flows(self) -> List[MitmFlow]:
        """Return all captured flows so far.

        Reads ``/tmp/mitm_flows.json`` from the proxy container (written
        continuously by the mitmproxy addon).  Returns an empty list if no
        traffic has been captured yet.
        """
        result = await self._shell.run("cat /tmp/mitm_flows.json 2>/dev/null || echo '[]'")
        try:
            raw: List[Dict[str, Any]] = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError:
            return []
        return [MitmFlow.from_dict(d) for d in raw]

    async def export_flows(self, local_path: str) -> None:
        """Save the binary mitmproxy flow file to *local_path* on the host."""
        import base64
        from pathlib import Path

        result = await self._shell.run("base64 /tmp/mitm_flows.bin 2>/dev/null || echo ''")
        b64 = result.stdout.strip()
        if b64:
            Path(local_path).write_bytes(base64.b64decode(b64))

    async def clear(self) -> None:
        """Clear the captured flow log."""
        await self._shell.run("echo '[]' > /tmp/mitm_flows.json && truncate -s 0 /tmp/mitm_flows.bin 2>/dev/null || true")


# ── builder ──────────────────────────────────────────────────────────────────


class MitmProxy:
    """Builder that returns pre-configured mitmproxy ``Image`` sidecars.

    Every classmethod returns a plain ``Image`` that can be passed to
    ``Image.with_proxy()``::

        topology = Image.linux().with_proxy(MitmProxy.replace(...))
    """

    @classmethod
    def image(cls) -> "Image":
        """Bare transparent proxy — captures all traffic, no modifications."""
        return cls._base_image(_FLOW_LOG_ADDON)

    @classmethod
    def replace(cls, url_pattern: str, search: str, replacement: str) -> "Image":
        """Transparent proxy that rewrites matching response bodies.

        Args:
            url_pattern: A Python ``re``-compatible pattern matched against the
                         full request URL (e.g. ``"example\\.com"``).
            search:      Literal string to find in the response body.
            replacement: String to replace every occurrence of *search* with.
        """
        addon = _REPLACE_ADDON_TEMPLATE.format(
            pattern=url_pattern,
            search=search,
            replace=replacement,
        )
        return cls._base_image(addon)

    @classmethod
    def script(cls, addon_code: str) -> "Image":
        """Transparent proxy running a custom mitmproxy addon.

        The addon must define at least one of the standard mitmproxy event
        hooks (``request``, ``response``, ``tls_start_client``, …).

        To also capture flows to ``/tmp/mitm_flows.json`` (needed for
        ``sb.proxy.flows()``), include the logging block from
        ``MitmProxy.image()`` in your script.
        """
        return cls._base_image(addon_code)

    # ── internal ────────────────────────────────────────────────────────────

    @classmethod
    def _base_image(cls, addon_code: str) -> "Image":
        from cua_sandbox.image import Image

        # Write the addon and entrypoint via shell-safe heredoc-equivalent:
        # Use printf + base64 so arbitrary Python code doesn't break the
        # shell command quoting.
        import base64 as _b64

        addon_b64 = _b64.b64encode(addon_code.encode()).decode()
        entrypoint_b64 = _b64.b64encode(_ENTRYPOINT_SH.encode()).decode()

        return (
            Image.base("python:3.12-slim")
            # pip3 + --break-system-packages works on both python:3.12-slim and Ubuntu
            .run("pip3 install --quiet --break-system-packages --ignore-installed mitmproxy")
            # Write addon script
            .run(f"echo {shlex.quote(addon_b64)} | base64 -d > /mitm_addon.py")
            # Write entrypoint (used in standalone multi-container mode)
            .run(
                f"echo {shlex.quote(entrypoint_b64)} | base64 -d > /entrypoint.sh"
                " && chmod +x /entrypoint.sh"
            )
            # Pre-generate CA cert so it exists before mitmdump starts in proxy mode.
            # Runs mitmdump briefly on a throwaway port, waits for it to write
            # ~/.mitmproxy/mitmproxy-ca-cert.pem, then kills it.
            .run(
                "mitmdump --listen-port 19999 &"
                " MPID=$!"
                " && sleep 4"
                " && kill $MPID 2>/dev/null"
                " ; ls /root/.mitmproxy/mitmproxy-ca-cert.pem"
            )
        )
