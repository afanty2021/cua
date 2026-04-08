"""Parse a mitmproxy flows file and generate a Swagger/Redoc-style HTML reference.

Usage
-----
    uv run examples/sandboxes/mitm_flows_to_docs.py [FLOWS_FILE] [OUT_HTML]

Defaults:
    FLOWS_FILE = /tmp/mitm_demo/mitm_flows.bin
    OUT_HTML   = /tmp/mitm_demo/api_docs.html

Dependencies: mitmproxy  (pip install mitmproxy)
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import sys
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── data model ───────────────────────────────────────────────────────────────


@dataclass
class ParsedBody:
    content_type: str
    raw: bytes
    form: Optional[dict]  # if application/x-www-form-urlencoded
    json_obj: Optional[object]  # if application/json
    text: Optional[str]  # if human-readable text
    is_binary: bool


@dataclass
class FlowRecord:
    method: str
    scheme: str
    host: str
    path: str
    query: str
    req_headers: dict
    req_body: ParsedBody
    status_code: Optional[int]
    resp_headers: dict
    resp_body: Optional[ParsedBody]
    is_tcp: bool = False  # pure TCP tunnel, no HTTP decode


@dataclass
class EndpointGroup:
    method: str
    scheme: str
    host: str
    path: str
    flows: list[FlowRecord] = field(default_factory=list)

    @property
    def endpoint_id(self) -> str:
        h = hashlib.md5(f"{self.method}{self.scheme}{self.host}{self.path}".encode()).hexdigest()[
            :8
        ]
        return f"ep-{h}"

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}{self.path}"

    @property
    def status_codes(self) -> list[int]:
        return sorted({f.status_code for f in self.flows if f.status_code})


# ── body parsing ─────────────────────────────────────────────────────────────


def _parse_body(raw: bytes, content_type: str) -> ParsedBody:
    ct = content_type.lower().split(";")[0].strip()
    is_binary = False
    form = None
    json_obj = None
    text = None

    if not raw:
        return ParsedBody(ct, raw, None, None, None, False)

    if ct == "application/x-www-form-urlencoded":
        try:
            decoded = raw.decode("utf-8", errors="strict")
            form = {
                k: v[0] if len(v) == 1 else v
                for k, v in urllib.parse.parse_qs(decoded, keep_blank_values=True).items()
            }
            text = decoded
        except Exception:
            is_binary = True

    elif ct in ("application/json", "text/json"):
        try:
            text = raw.decode("utf-8", errors="strict")
            json_obj = json.loads(text)
        except Exception:
            is_binary = True

    elif ct.startswith("text/"):
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            is_binary = True

    else:
        # Try utf-8; if > 30% non-printable assume binary
        try:
            candidate = raw.decode("utf-8", errors="strict")
            non_print = sum(1 for c in candidate if not c.isprintable() and c not in "\r\n\t")
            if non_print / max(len(candidate), 1) > 0.30:
                is_binary = True
            else:
                text = candidate
        except Exception:
            is_binary = True

    return ParsedBody(ct, raw, form, json_obj, text, is_binary)


# ── flow parsing ──────────────────────────────────────────────────────────────


def load_flows(path: Path) -> list[FlowRecord]:
    import warnings

    warnings.filterwarnings("ignore")  # suppress CryptographyDeprecationWarning from mitmproxy
    from mitmproxy import io as mio

    records: list[FlowRecord] = []
    with open(path, "rb") as fh:
        reader = mio.FlowReader(fh)
        for flow in reader.stream():
            try:
                # TCP tunnel flows (no HTTP decode)
                if flow.__class__.__name__ == "TCPFlow":
                    records.append(
                        FlowRecord(
                            method="TCP",
                            scheme="tcp",
                            host=flow.server_conn.address[0] if flow.server_conn else "?",
                            path="",
                            query="",
                            req_headers={},
                            req_body=ParsedBody("", b"", None, None, None, True),
                            status_code=None,
                            resp_headers={},
                            resp_body=None,
                            is_tcp=True,
                        )
                    )
                    continue

                if not hasattr(flow, "request"):
                    continue

                req = flow.request
                resp = flow.response

                req_ct = req.headers.get("content-type", "")
                req_body = _parse_body(req.content or b"", req_ct)

                resp_body: Optional[ParsedBody] = None
                if resp:
                    resp_ct = resp.headers.get("content-type", "")
                    resp_body = _parse_body(resp.content or b"", resp_ct)

                records.append(
                    FlowRecord(
                        method=req.method,
                        scheme=req.scheme,
                        host=req.pretty_host,
                        path=req.path.split("?")[0],
                        query=urllib.parse.urlencode(req.query),
                        req_headers=dict(req.headers),
                        req_body=req_body,
                        status_code=resp.status_code if resp else None,
                        resp_headers=dict(resp.headers) if resp else {},
                        resp_body=resp_body,
                    )
                )
            except Exception:
                pass  # skip malformed/truncated individual flow

    return records


def group_flows(records: list[FlowRecord]) -> list[EndpointGroup]:
    groups: dict[tuple, EndpointGroup] = {}
    for r in records:
        key = (r.method, r.scheme, r.host, r.path)
        if key not in groups:
            groups[key] = EndpointGroup(r.method, r.scheme, r.host, r.path)
        groups[key].flows.append(r)
    return list(groups.values())


# ── HTML rendering ────────────────────────────────────────────────────────────

STATUS_COLORS = {
    2: "#22c55e",  # green
    3: "#f59e0b",  # amber
    4: "#ef4444",  # red
    5: "#ef4444",
}

METHOD_COLORS = {
    "GET": "#22c55e",
    "POST": "#3b82f6",
    "PUT": "#f59e0b",
    "PATCH": "#a855f7",
    "DELETE": "#ef4444",
    "HEAD": "#6b7280",
    "TCP": "#6b7280",
}


def _esc(s: str) -> str:
    return html.escape(str(s))


def _method_badge(method: str) -> str:
    color = METHOD_COLORS.get(method, "#6b7280")
    return f'<span class="method-badge" style="background:{color}">' f"{_esc(method)}</span>"


def _status_badge(code: int) -> str:
    color = STATUS_COLORS.get(code // 100, "#6b7280")
    return f'<span class="status-badge" style="background:{color}">' f"{_esc(str(code))}</span>"


def _render_body(body: ParsedBody, label: str, collapsed: bool = False) -> str:
    if not body.raw:
        return f'<div class="body-empty"><em>No {label.lower()} body</em></div>'

    parts = [
        '<div class="body-section">',
        f'<div class="body-meta">Content-Type: <code>{_esc(body.content_type)}</code>'
        f" &nbsp; Size: <code>{len(body.raw):,} bytes</code></div>",
    ]

    if body.is_binary:
        b64 = base64.b64encode(body.raw).decode()
        hex_preview = body.raw[:256].hex()
        hex_lines = [hex_preview[i : i + 32] for i in range(0, len(hex_preview), 32)]
        hex_block = "\n".join(
            f"{i*16:08x}  {line[:16*2]:32s}  {''.join(chr(b) if 32 <= b < 127 else '.' for b in bytes.fromhex(line))}"
            for i, line in enumerate(hex_lines)
        )
        parts.append(
            f'<details {"" if collapsed else "open"}>'
            f'<summary class="section-toggle">Binary body ({len(body.raw):,} bytes)</summary>'
            f'<pre class="hex-dump">{_esc(hex_block)}</pre>'
            f'<div class="b64-block"><small>Base64:</small>'
            f'<code class="b64">{_esc(b64[:120])}{"..." if len(b64)>120 else ""}</code></div>'
            f"</details>"
        )

    elif body.form is not None:
        rows = "".join(
            f'<tr><td class="field-name"><code>{_esc(k)}</code></td>'
            f'<td class="field-value"><code>{_esc(str(v))}</code></td></tr>'
            for k, v in body.form.items()
        )
        parts.append(
            f'<details {"" if collapsed else "open"}>'
            f'<summary class="section-toggle">Form fields ({len(body.form)} params)</summary>'
            f'<table class="form-table"><thead><tr><th>Field</th><th>Value</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
            f"</details>"
        )
        # Also show raw
        parts.append(
            f'<details><summary class="section-toggle muted">Raw URL-encoded</summary>'
            f'<pre class="raw-body">{_esc((body.text or "")[:2000])}</pre></details>'
        )

    elif body.json_obj is not None:
        pretty = json.dumps(body.json_obj, indent=2, ensure_ascii=False)
        parts.append(
            f'<details {"" if collapsed else "open"}>'
            f'<summary class="section-toggle">JSON body</summary>'
            f'<pre class="json-body">{_esc(pretty[:4000])}</pre></details>'
        )

    elif body.text:
        # Detect if it looks like HTML (response error pages, etc.)
        is_html = body.text.lstrip().startswith("<!DOCTYPE") or body.text.lstrip().startswith(
            "<html"
        )
        if is_html:
            parts.append(
                f'<details><summary class="section-toggle">HTML response '
                f"({len(body.raw):,} bytes)</summary>"
                f'<pre class="raw-body">{_esc(body.text[:2000])}</pre></details>'
            )
        else:
            parts.append(
                f'<details {"" if collapsed else "open"}>'
                f'<summary class="section-toggle">Body</summary>'
                f'<pre class="raw-body">{_esc(body.text[:4000])}</pre></details>'
            )

    parts.append("</div>")
    return "\n".join(parts)


def _render_headers(headers: dict) -> str:
    if not headers:
        return '<em class="muted">None</em>'
    rows = "".join(
        f'<tr><td class="hdr-name"><code>{_esc(k)}</code></td>'
        f'<td class="hdr-value"><code>{_esc(v)}</code></td></tr>'
        for k, v in headers.items()
    )
    return f'<table class="hdr-table"><tbody>{rows}</tbody></table>'


def _render_query(query: str) -> str:
    if not query:
        return '<em class="muted">None</em>'
    try:
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        rows = "".join(
            f'<tr><td class="field-name"><code>{_esc(k)}</code></td>'
            f'<td class="field-value"><code>{_esc(", ".join(v))}</code></td></tr>'
            for k, v in params.items()
        )
        return (
            f'<table class="form-table">'
            f"<thead><tr><th>Parameter</th><th>Value</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    except Exception:
        return f"<code>{_esc(query)}</code>"


def _render_endpoint(group: EndpointGroup) -> str:
    eid = group.endpoint_id
    status_badges = (
        " ".join(_status_badge(s) for s in group.status_codes)
        or '<em class="muted">No response captured</em>'
    )
    call_count = len(group.flows)

    # Aggregate unique request field names across all flows for the summary table
    all_fields: dict[str, set] = defaultdict(set)
    for f in group.flows:
        if f.req_body.form:
            for k, v in f.req_body.form.items():
                all_fields[k].add(str(v)[:80])

    field_table = ""
    if all_fields:
        rows = "".join(
            f'<tr><td class="field-name"><code>{_esc(k)}</code></td>'
            f'<td class="field-values">'
            + ", ".join(f"<code>{_esc(v)}</code>" for v in sorted(vs))
            + "</td></tr>"
            for k, vs in sorted(all_fields.items())
        )
        field_table = (
            f"<h4>Observed request parameters</h4>"
            f'<table class="form-table params-summary">'
            f"<thead><tr><th>Field</th><th>Observed values</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    # Individual flow examples (collapsed by default if many)
    flow_examples = []
    for i, f in enumerate(group.flows):
        status_html = (
            _status_badge(f.status_code) if f.status_code else '<em class="muted">No response</em>'
        )
        q_html = _render_query(f.query)
        req_hdr_html = _render_headers(f.req_headers)
        req_body_html = _render_body(f.req_body, "Request", collapsed=(i > 0))
        resp_hdr_html = (
            _render_headers(f.resp_headers) if f.resp_headers else '<em class="muted">None</em>'
        )
        resp_body_html = (
            _render_body(f.resp_body, "Response", collapsed=(i > 0))
            if f.resp_body
            else '<div class="body-empty"><em>No response captured</em></div>'
        )

        flow_examples.append(
            f"""
        <details {"open" if i == 0 else ""} class="flow-example">
          <summary class="flow-summary">
            Example #{i+1} &nbsp; {status_html}
          </summary>
          <div class="flow-detail">
            <div class="flow-half">
              <h5>Request</h5>
              <div class="subsection-label">Query string</div>{q_html}
              <div class="subsection-label">Headers</div>{req_hdr_html}
              <div class="subsection-label">Body</div>{req_body_html}
            </div>
            <div class="flow-half">
              <h5>Response &nbsp; {status_html}</h5>
              <div class="subsection-label">Headers</div>{resp_hdr_html}
              <div class="subsection-label">Body</div>{resp_body_html}
            </div>
          </div>
        </details>
        """
        )

    flows_html = "\n".join(flow_examples)

    tcp_note = ""
    if group.is_tcp if hasattr(group, "is_tcp") else False:
        tcp_note = '<div class="tcp-note">Raw TCP tunnel — payload not decoded</div>'

    return f"""
    <section class="endpoint" id="{eid}">
      <div class="endpoint-header">
        {_method_badge(group.method)}
        <span class="endpoint-url">{_esc(group.scheme)}://<strong>{_esc(group.host)}</strong>{_esc(group.path)}</span>
        <span class="endpoint-meta">{call_count} call{"s" if call_count != 1 else ""} &nbsp; {status_badges}</span>
      </div>
      <div class="endpoint-body">
        {tcp_note}
        {field_table}
        <h4>Captured flows</h4>
        {flows_html}
      </div>
    </section>
    """


def _render_tcp_group(host: str, count: int) -> str:
    return f"""
    <section class="endpoint tcp-endpoint">
      <div class="endpoint-header">
        {_method_badge("TCP")}
        <span class="endpoint-url">tcp://<strong>{_esc(host)}</strong></span>
        <span class="endpoint-meta">{count} tunnel{"s" if count != 1 else ""} &nbsp;
          <span class="status-badge" style="background:#6b7280">TUNNEL</span>
        </span>
      </div>
      <div class="endpoint-body">
        <div class="tcp-note">Raw TCP tunnels — TLS was intercepted but payload uses a binary
        protocol (e.g. Telegram MTProto). Packet count: <strong>{count}</strong>.</div>
      </div>
    </section>
    """


# ── nav sidebar ───────────────────────────────────────────────────────────────


def _nav_entry(group: EndpointGroup) -> str:
    color = METHOD_COLORS.get(group.method, "#6b7280")
    short_path = group.path[:40] + ("…" if len(group.path) > 40 else "")
    return (
        f'<a href="#{group.endpoint_id}" class="nav-item">'
        f'<span class="nav-badge" style="background:{color}">{_esc(group.method)}</span>'
        f'<span class="nav-host">{_esc(group.host)}</span>'
        f'<span class="nav-path">{_esc(short_path) or "/"}</span>'
        f"</a>"
    )


# ── full page ─────────────────────────────────────────────────────────────────


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f1117; color: #e2e8f0; display: flex; min-height: 100vh; }

/* sidebar */
#sidebar { width: 280px; min-width: 280px; background: #1a1d27; border-right: 1px solid #2d3148;
           overflow-y: auto; position: sticky; top: 0; height: 100vh; padding: 0; }
#sidebar-header { padding: 20px 16px 12px; border-bottom: 1px solid #2d3148; }
#sidebar-header h1 { font-size: 15px; font-weight: 700; color: #a78bfa; letter-spacing: .5px; }
#sidebar-header p  { font-size: 11px; color: #64748b; margin-top: 4px; }
.nav-item { display: flex; align-items: center; gap: 6px; padding: 7px 14px;
            text-decoration: none; color: #cbd5e1; font-size: 12px;
            border-bottom: 1px solid #22253a; transition: background .15s; flex-wrap: nowrap; }
.nav-item:hover { background: #252840; color: #e2e8f0; }
.nav-badge { font-size: 10px; font-weight: 700; padding: 2px 6px; border-radius: 4px;
             color: #fff; min-width: 44px; text-align: center; flex-shrink: 0; }
.nav-host  { color: #94a3b8; font-size: 11px; flex-shrink: 0; max-width: 90px;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nav-path  { color: #64748b; font-size: 11px; overflow: hidden; text-overflow: ellipsis;
             white-space: nowrap; }

/* main */
#main { flex: 1; padding: 32px; overflow-y: auto; max-width: calc(100% - 280px); }
#main-header { margin-bottom: 32px; }
#main-header h1 { font-size: 28px; font-weight: 800; color: #f1f5f9; }
#main-header p  { color: #64748b; margin-top: 6px; font-size: 14px; }
.stats-row { display: flex; gap: 16px; margin-top: 16px; flex-wrap: wrap; }
.stat-card { background: #1a1d27; border: 1px solid #2d3148; border-radius: 8px;
             padding: 12px 20px; min-width: 120px; }
.stat-card .num { font-size: 28px; font-weight: 700; color: #a78bfa; }
.stat-card .lbl { font-size: 12px; color: #64748b; margin-top: 2px; }

/* endpoint cards */
.endpoint { background: #1a1d27; border: 1px solid #2d3148; border-radius: 10px;
            margin-bottom: 24px; overflow: hidden; scroll-margin-top: 16px; }
.tcp-endpoint { opacity: .75; }
.endpoint-header { display: flex; align-items: center; gap: 12px; padding: 14px 20px;
                   background: #1e2235; border-bottom: 1px solid #2d3148; flex-wrap: wrap; }
.method-badge { font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 5px;
                color: #fff; min-width: 56px; text-align: center; flex-shrink: 0; }
.status-badge { font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 4px;
                color: #fff; }
.endpoint-url { font-size: 14px; font-weight: 600; color: #e2e8f0; word-break: break-all; flex: 1; }
.endpoint-url strong { color: #a78bfa; }
.endpoint-meta { font-size: 12px; color: #64748b; display: flex; align-items: center; gap: 6px;
                 flex-wrap: wrap; flex-shrink: 0; }
.endpoint-body { padding: 16px 20px; }

/* sub-sections */
h4 { font-size: 13px; font-weight: 700; color: #94a3b8; text-transform: uppercase;
     letter-spacing: .5px; margin: 20px 0 10px; }
h5 { font-size: 13px; font-weight: 700; color: #94a3b8; margin-bottom: 10px; }
.subsection-label { font-size: 11px; text-transform: uppercase; letter-spacing: .6px;
                    color: #475569; margin: 12px 0 5px; }

/* flow examples */
.flow-example { border: 1px solid #2d3148; border-radius: 7px; margin-bottom: 10px; overflow: hidden; }
.flow-summary { padding: 10px 14px; cursor: pointer; font-size: 13px; font-weight: 600;
                color: #94a3b8; background: #1a1d27; display: flex; align-items: center; gap: 8px;
                list-style: none; user-select: none; }
.flow-summary:hover { background: #22253a; }
.flow-detail { display: flex; gap: 0; }
.flow-half { flex: 1; padding: 14px 16px; border-right: 1px solid #2d3148; }
.flow-half:last-child { border-right: none; }

/* tables */
.form-table, .hdr-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 4px; }
.form-table th { background: #252840; padding: 6px 10px; text-align: left; color: #64748b;
                 font-weight: 600; font-size: 11px; border: 1px solid #2d3148; }
.form-table td, .hdr-table td { padding: 5px 10px; border: 1px solid #1e2235; vertical-align: top; }
.field-name code, .hdr-name code { color: #7dd3fc; }
.field-value code, .hdr-value code { color: #fbbf24; word-break: break-all; }
.field-values code { color: #fbbf24; margin-right: 4px; }
.params-summary { margin-bottom: 12px; }

/* code/pre */
pre { background: #0d1117; border: 1px solid #1e2235; border-radius: 6px; padding: 12px;
      font-size: 11px; overflow-x: auto; color: #e2e8f0; line-height: 1.6; margin-top: 4px; }
pre.hex-dump { color: #94a3b8; font-family: 'Fira Mono', 'Menlo', monospace; }
pre.json-body { color: #86efac; }
pre.raw-body { color: #fde68a; }
code { font-family: 'Fira Mono', 'Menlo', 'Courier New', monospace; font-size: 12px; }
.b64-block { margin-top: 8px; }
.b64-block code { color: #64748b; font-size: 10px; word-break: break-all; display: block;
                  background: #0d1117; padding: 6px; border-radius: 4px; margin-top: 3px; }

/* misc */
.section-toggle { cursor: pointer; font-size: 12px; color: #7dd3fc; padding: 5px 0;
                  display: block; user-select: none; }
.section-toggle:hover { color: #93c5fd; }
.section-toggle.muted { color: #475569; }
.body-empty { color: #475569; font-style: italic; font-size: 12px; padding: 6px 0; }
.body-section { margin-top: 4px; }
.body-meta { font-size: 11px; color: #64748b; margin-bottom: 6px; }
.tcp-note { background: #1e2235; border-left: 3px solid #6b7280; padding: 8px 12px;
            font-size: 12px; color: #94a3b8; border-radius: 0 5px 5px 0; margin: 8px 0; }
.muted { color: #475569; }
.section-divider { border: none; border-top: 1px solid #1e2235; margin: 28px 0; }

details > summary { list-style: none; }
details > summary::-webkit-details-marker { display: none; }
"""


def generate_html(flows_path: Path, out_path: Path) -> None:
    print(f"Loading flows from {flows_path} ...")
    records = load_flows(flows_path)
    print(f"  {len(records)} flow records loaded")

    http_records = [r for r in records if not r.is_tcp]
    tcp_records = [r for r in records if r.is_tcp]

    groups = group_flows(http_records)
    print(f"  {len(groups)} unique HTTP endpoints, {len(tcp_records)} TCP tunnels")

    # Sort: by host then path
    groups.sort(key=lambda g: (g.host, g.path, g.method))

    # Aggregate TCP by host
    tcp_by_host: dict[str, int] = defaultdict(int)
    for r in tcp_records:
        tcp_by_host[r.host] += 1

    # Build nav
    nav_items = "\n".join(_nav_entry(g) for g in groups)
    if tcp_by_host:
        nav_items += '\n<div style="padding:8px 14px;font-size:10px;color:#475569;text-transform:uppercase;letter-spacing:.5px">TCP Tunnels</div>'
        for host, cnt in sorted(tcp_by_host.items()):
            color = METHOD_COLORS["TCP"]
            nav_items += (
                f'<a href="#tcp-{html.escape(host)}" class="nav-item">'
                f'<span class="nav-badge" style="background:{color}">TCP</span>'
                f'<span class="nav-host">{html.escape(host)}</span>'
                f'<span class="nav-path">{cnt} tunnel{"s" if cnt!=1 else ""}</span>'
                f"</a>"
            )

    # Build endpoint sections
    endpoint_sections = "\n".join(_render_endpoint(g) for g in groups)

    tcp_sections = ""
    for host, cnt in sorted(tcp_by_host.items()):
        tcp_sections += f'<div id="tcp-{html.escape(host)}">{_render_tcp_group(host, cnt)}</div>'

    # Stats
    total_hosts = len({g.host for g in groups} | set(tcp_by_host.keys()))
    total_flows = len(http_records) + len(tcp_records)
    status_dist: dict[int, int] = defaultdict(int)
    for r in http_records:
        if r.status_code:
            status_dist[r.status_code] += 1
    status_stats = " &nbsp; ".join(
        f'<strong style="color:{STATUS_COLORS.get(k//100,"#6b7280")}">{k}</strong>×{v}'
        for k, v in sorted(status_dist.items())
    )

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Intercepted API Reference — {flows_path.name}</title>
  <style>{CSS}</style>
</head>
<body>
  <nav id="sidebar">
    <div id="sidebar-header">
      <h1>⚡ Intercepted APIs</h1>
      <p>{flows_path.name} &nbsp;·&nbsp; {total_flows} flows</p>
    </div>
    {nav_items}
  </nav>

  <main id="main">
    <div id="main-header">
      <h1>Intercepted API Reference</h1>
      <p>Auto-generated from mitmproxy capture &nbsp;·&nbsp;
         <code>{flows_path.resolve()}</code></p>
      <div class="stats-row">
        <div class="stat-card"><div class="num">{len(groups)}</div><div class="lbl">Endpoints</div></div>
        <div class="stat-card"><div class="num">{total_hosts}</div><div class="lbl">Hosts</div></div>
        <div class="stat-card"><div class="num">{total_flows}</div><div class="lbl">Total flows</div></div>
        <div class="stat-card"><div class="num">{len(tcp_by_host)}</div><div class="lbl">TCP tunnels</div></div>
      </div>
      <div style="margin-top:12px;font-size:12px;color:#64748b">
        Status codes: {status_stats or "none"}
      </div>
    </div>

    {endpoint_sections}

    {"<hr class='section-divider'><h2 style='color:#64748b;margin-bottom:16px'>TCP Tunnels</h2>" if tcp_sections else ""}
    {tcp_sections}
  </main>
</body>
</html>"""

    out_path.write_text(page, encoding="utf-8")
    print(f"Generated → {out_path}  ({len(page):,} bytes)")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    flows_file = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/mitm_demo/mitm_flows.bin")
    out_file = Path(sys.argv[2]) if len(sys.argv) > 2 else flows_file.parent / "api_docs.html"

    if not flows_file.exists():
        raise SystemExit(f"Flows file not found: {flows_file}")

    generate_html(flows_file, out_file)
    print(f"\nOpen in browser:  open {out_file}")
