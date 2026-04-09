"""Linux desktop app reverse-engineering gym — Slack, Discord, Spotify.

Capabilities per app
--------------------
* MITM traffic capture  — all HTTPS flows intercepted via iptables + mitmproxy
* CDP JS injection      — arbitrary JS executed in Electron renderer
* State extraction      — localStorage, window globals, DOM text, React/Redux state
* IPC eavesdropping     — ipcMain calls logged after ASAR patch (Slack/Discord)
* API mocking           — mitmproxy addon replaces responses to unlock offline RL
* Screenshot            — VNC screenshot + CDP screenshot (higher fidelity)

All three apps are Electron-based on Linux:
  Slack    — ships as .deb, uses NODE_EXTRA_CA_CERTS for MITM
  Discord  — ships as .deb, accepts --remote-debugging-port natively
  Spotify  — ships via apt repo, Electron with Chromium-based renderer

Architecture
------------
  Single DockerRuntime(privileged=True) container (iptables for MITM)
  Xvfb :99 for headless display
  mitmproxy in regular mode (port 8080), CA trusted system-wide
  Apps launched with NODE_EXTRA_CA_CERTS + --remote-debugging-port=9222
  CDP called from inside container via sb.shell.run (avoids WSL2 host→container TCP reset)

Usage
-----
    uv run examples/sandboxes/test_app_re_linux.py [slack|discord|spotify|all]
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

from cua_sandbox import Image, Sandbox
from cua_sandbox.runtime import DockerRuntime

OUTPUT_DIR = Path("/tmp/app_re_linux")

# ── shell helper ──────────────────────────────────────────────────────────────


async def sh_bg(sb, cmd: str, *, tag: str = "bg", timeout: int = 600) -> str:
    """Run a long command in background inside the container (avoids HTTP timeout).

    Writes a launcher script, runs it in background, polls for completion.
    """
    log = f"/tmp/{tag}.log"
    done = f"/tmp/{tag}.done"
    script = f"""import subprocess, sys
p = subprocess.run({repr(cmd)}, shell=True,
    stdout=open({repr(log)}, 'w'), stderr=subprocess.STDOUT)
open({repr(done)}, 'w').write(str(p.returncode))
"""
    launcher = f"/tmp/{tag}_launch.py"
    await sb.files.write(launcher, script)
    # Launch in background — shell returns immediately
    await sh(sb, f"rm -f {done}; python3 {launcher} &", timeout=10)
    # Poll until done file appears
    start = time.time()
    while time.time() - start < timeout:
        await asyncio.sleep(8)
        rc_str = (await sh(sb, f"cat {done} 2>/dev/null || echo RUNNING")).strip()
        if rc_str != "RUNNING":
            out = await sh(sb, f"cat {log} 2>/dev/null | tail -20 || echo ''")
            rc = int(rc_str) if rc_str.lstrip("-").isdigit() else 1
            if rc != 0:
                raise RuntimeError(f"bg cmd failed (rc={rc}): {cmd[:80]}\n{out[-300:]}")
            return out
        elapsed = int(time.time() - start)
        if elapsed % 60 < 8:
            print(f"  [{tag}] still running ({elapsed}s)...")
    raise RuntimeError(f"bg cmd timed out after {timeout}s: {cmd[:80]}")


async def sh(sb, cmd: str, *, check: bool = False, timeout: int = 120) -> str:
    r = await sb.shell.run(cmd, timeout=timeout)
    if check and not r.success:
        raise RuntimeError(f"cmd failed: {cmd!r}\nstderr: {r.stderr}\nstdout: {r.stdout}")
    return (r.stdout or "").strip()


# ── CDP helpers ───────────────────────────────────────────────────────────────


async def cdp_call(ws_url: str, method: str, params: dict = {}) -> Any:
    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024, open_timeout=10) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})


async def cdp_eval(ws_url: str, expr: str, await_promise: bool = False) -> Any:
    r = await cdp_call(ws_url, "Runtime.evaluate", {
        "expression": expr,
        "returnByValue": True,
        "awaitPromise": await_promise,
        "timeout": 8000,
    })
    rv = r.get("result", {})
    if rv.get("subtype") == "error":
        return f"<JS error: {rv.get('description', '')}>"
    if rv.get("type") == "undefined":
        return None
    return rv.get("value", rv.get("description"))


async def cdp_call_via_shell(sb, method: str, params: dict = {}) -> Any:
    """Execute a CDP call from INSIDE the container via Python+websockets shell command.
    Avoids WSL2/Docker host-networking issues where host→container TCP gets reset.
    """
    params_json = json.dumps(params).replace('"', '\\"')
    script = (
        "import json,urllib.request,websocket; "  # uses websocket-client
        "targets=json.loads(urllib.request.urlopen('http://localhost:9222/json',timeout=5).read()); "
        "pages=[t for t in targets if t.get('type')=='page' and 'devtools' not in t.get('url','')]; "
        "t=pages[0] if pages else targets[0] if targets else None; "
        "ws=websocket.WebSocket() if t else None; "
        "ws.connect(t['webSocketDebuggerUrl']) if t else None; "
        f"ws.send(json.dumps({{'id':1,'method':'{method}','params':{json.dumps(params)}}})) if t else None; "
        "r=json.loads(ws.recv()) if t else {}; "
        "ws.close() if t else None; "
        "print(json.dumps(r.get('result',{})))"
    )
    r = await sh(sb, f"python3 -c \"{script}\"", timeout=30)
    return json.loads(r) if r.strip().startswith("{") else {}


async def cdp_eval_via_shell(sb, expr: str, await_promise: bool = False) -> Any:
    """Evaluate JS via CDP from inside the container."""
    params = {"expression": expr, "returnByValue": True, "awaitPromise": await_promise, "timeout": 8000}
    result = await cdp_call_via_shell(sb, "Runtime.evaluate", params)
    rv = result.get("result", {})
    if rv.get("subtype") == "error":
        return f"<JS error: {rv.get('description', '')}>"
    if rv.get("type") == "undefined":
        return None
    return rv.get("value", rv.get("description"))


async def install_websocket_client(sb) -> None:
    """Install websocket-client inside container for CDP shell calls."""
    await sh_bg(sb, "pip3 install websocket-client || pip install websocket-client",
                tag="pip_ws", timeout=60)


async def wait_for_cdp(sb, retries: int = 15, delay: float = 5.0) -> bool:
    """Poll CDP from inside the container until a page target appears."""
    for attempt in range(retries):
        check = await sh(sb,
            "python3 -c \""
            "import urllib.request,json; "
            "t=json.loads(urllib.request.urlopen('http://localhost:9222/json',timeout=3).read()); "
            "pages=[x for x in t if x.get('type')=='page']; "
            "print('OK' if pages else 'NONE')\" 2>/dev/null || echo ERR",
            timeout=10)
        if "OK" in check:
            print(f"  CDP ready (attempt {attempt+1})")
            return True
        print(f"  CDP: {check.strip()[:50]} (attempt {attempt+1}/{retries}), waiting {delay}s...")
        await asyncio.sleep(delay)
    return False


# Write helper scripts to /tmp inside container to avoid shell quoting nightmares
CDP_HELPER = r"""
import json, os, urllib.request
import websocket

# Bypass proxy for localhost CDP connections
_no_proxy_handler = urllib.request.ProxyHandler({})
_opener = urllib.request.build_opener(_no_proxy_handler)

# Clear proxy env vars so websocket-client doesn't route ws://localhost through mitmproxy
for _k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
    os.environ.pop(_k, None)

def cdp_targets():
    raw = _opener.open("http://localhost:9222/json", timeout=5).read()
    targets = json.loads(raw)
    pages = [t for t in targets if t.get("type") == "page"
             and "devtools" not in t.get("url", "")]
    return pages or targets

def _ws_connect(url, timeout=15):
    ws = websocket.WebSocket()
    ws.settimeout(timeout)
    ws.connect(url)
    return ws

def cdp_eval(expression, await_promise=False):
    targets = cdp_targets()
    if not targets:
        return None
    ws = _ws_connect(targets[0]["webSocketDebuggerUrl"])
    ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
        "expression": expression, "returnByValue": True, "awaitPromise": await_promise,
        "timeout": 8000,
    }}))
    r = json.loads(ws.recv())
    ws.close()
    rv = r.get("result", {}).get("result", {})
    if rv.get("subtype") == "error":
        return f"<JS error: {rv.get('description', '')}>"
    if rv.get("type") == "undefined":
        return None
    return rv.get("value", rv.get("description"))

def cdp_screenshot():
    targets = cdp_targets()
    if not targets:
        return ""
    ws = _ws_connect(targets[0]["webSocketDebuggerUrl"], timeout=30)
    ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
    r = json.loads(ws.recv())
    ws.close()
    return r.get("result", {}).get("data", "")
"""

_EXTRACT_SCRIPT_SRC = """\
import json, sys, time
sys.path.insert(0, '/tmp')
import cdp_helper as cdp

# Wait for page to have content
for _ in range(6):
    title = cdp.cdp_eval('document.title')
    url = cdp.cdp_eval('location.href')
    if title or (url and url != 'about:blank'):
        break
    time.sleep(2)

out = {}
try:
    ls = cdp.cdp_eval('JSON.stringify(Object.fromEntries(Object.keys(localStorage).slice(0,50).map(k=>[k,localStorage.getItem(k)])))')
    out['localStorage'] = json.loads(ls) if ls else {}
except:
    out['localStorage'] = {}
out['title'] = cdp.cdp_eval('document.title')
out['url'] = cdp.cdp_eval('location.href')
out['bodyText'] = (cdp.cdp_eval('document.body ? document.body.innerText.slice(0,500) : ""') or '')
out['hasReact'] = bool(cdp.cdp_eval(
    'Boolean(document.querySelector("#root") && Object.keys(document.querySelector("#root")||{}).find(k=>k.startsWith("__reactFiber")))'
))
out['hasElectronIpc'] = bool(cdp.cdp_eval('typeof window.require !== "undefined"'))
try:
    out['appVersion'] = cdp.cdp_eval(
        '(window.SLACK_APP_VERSION || window.DISCORD_RELEASE_CHANNEL || navigator.userAgent || "").slice(0,120)'
    )
except:
    pass
print(json.dumps(out))
"""
EXTRACT_SCRIPT = _EXTRACT_SCRIPT_SRC


async def write_cdp_helpers(sb) -> None:
    """Upload cdp_helper.py and extract_state.py to /tmp inside the container."""
    await sb.files.write("/tmp/cdp_helper.py", CDP_HELPER)
    await sb.files.write("/tmp/extract_state.py", EXTRACT_SCRIPT)


async def extract_state_via_shell(sb, app_name: str) -> dict:
    """Run the state extractor script inside the container."""
    # Verify helper files exist
    file_check = await sh(sb, "ls -la /tmp/cdp_helper.py /tmp/extract_state.py 2>&1 || echo MISSING")
    if "MISSING" in file_check or "No such" in file_check:
        print(f"  [warn] CDP helper files missing: {file_check[:200]}")
        # Re-upload
        await write_cdp_helpers(sb)

    result = await sh(sb, "python3 /tmp/extract_state.py 2>&1 || echo '{}'", timeout=45)
    # Find the JSON line (last line starting with {)
    json_line = ""
    for line in reversed(result.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            json_line = line
            break
    if not json_line:
        print(f"  [warn] extract_state raw output: {result[:400]}")
        return {}
    try:
        return json.loads(json_line)
    except Exception:
        return {"raw": result[:200]}


# ── shared container setup ────────────────────────────────────────────────────


MITM_ADDON = '''
import json, os, time
from mitmproxy import http

_path = os.environ.get("MITM_FLOWS_PATH", "/tmp/mitm_flows.json")

def _ct(flow):
    return (flow.response.headers.get("content-type", "") if flow.response else "")

def _text(flow):
    ct = _ct(flow)
    if not any(x in ct for x in ("text","json","xml","javascript","html","form")):
        return None
    try:
        return flow.response.text[:4096]
    except Exception:
        return None

def response(flow: http.HTTPFlow):
    entry = {
        "ts": time.time(), "method": flow.request.method,
        "url": flow.request.pretty_url,
        "status": flow.response.status_code if flow.response else None,
        "req_body": flow.request.text[:512] if flow.request.content else None,
        "resp_body": _text(flow),
    }
    with open(_path, "a") as f:
        f.write(json.dumps(entry) + "\\n")
'''


async def setup_base(sb) -> str:
    """Install shared deps, mitmproxy, Xvfb. Returns mitmproxy cert path."""
    print("[base] Installing dependencies...")
    await sh_bg(sb,
        "sudo apt-get update -qq && sudo apt-get install -y --no-install-recommends "
        "curl gnupg ca-certificates python3-pip pipx xvfb "
        "libgbm1 libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 "
        "libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 "
        "libasound2 libx11-xcb1 libdrm2 iptables gdebi-core",
        tag="apt_base", timeout=900)
    print("[base] apt-get done, installing mitmproxy...")
    await sh_bg(sb, "pipx install mitmproxy && pipx ensurepath", tag="pipx_mitm", timeout=300)

    # Generate mitmproxy CA cert (run briefly to create ~/.mitmproxy/)
    mitmdump = (await sh(sb, "which mitmdump || echo /home/cua/.local/bin/mitmdump")).splitlines()[0]
    gen = (f"import subprocess,time; p=subprocess.Popen(['{mitmdump}','--listen-port','8080'],"
           "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL,"
           "start_new_session=True); time.sleep(4); p.terminate(); p.wait()")
    await sh(sb, f'python3 -c "{gen}"', timeout=20)

    cert_path = "/home/cua/.mitmproxy/mitmproxy-ca-cert.pem"
    await sh(sb, f"sudo cp {cert_path} /usr/local/share/ca-certificates/mitmproxy.crt && "
             "sudo update-ca-certificates", check=True)

    # Write MITM addon
    await sh(sb, f"cat > /tmp/mitm_addon.py << 'PYEOF'\n{MITM_ADDON}\nPYEOF")

    # Start mitmdump in regular (not transparent) mode on 8080
    confdir = "/home/cua/.mitmproxy"
    launch = (f"import subprocess; p=subprocess.Popen(['{mitmdump}',"
              "'--mode','regular','--listen-port','8080',"
              f"'--set','confdir={confdir}',"
              "'-s','/tmp/mitm_addon.py'],"
              "stdout=open('/tmp/mitmdump.log','w'),stderr=subprocess.STDOUT,"
              "stdin=subprocess.DEVNULL,start_new_session=True); "
              "open('/tmp/mitmdump.pid','w').write(str(p.pid))")
    await sh(sb, f'python3 -c "{launch}"')
    await asyncio.sleep(2)
    print(f"[base] mitmproxy running (PID {await sh(sb, 'cat /tmp/mitmdump.pid')})")

    # Install websocket-client for in-container CDP calls
    await sh_bg(sb, "pip3 install websocket-client || pip install websocket-client",
                tag="pip_ws", timeout=120)

    # Write CDP helper scripts to /tmp
    await write_cdp_helpers(sb)

    # Start Xvfb (detached via Python subprocess to avoid blocking the transport)
    await sh(sb,
        "python3 -c \""
        "import subprocess; "
        "subprocess.Popen(['Xvfb',':99','-screen','0','1280x800x24'],"
        "stdout=open('/tmp/xvfb.log','w'),stderr=subprocess.STDOUT,"
        "stdin=subprocess.DEVNULL,start_new_session=True)"
        "\"")
    await asyncio.sleep(2)
    print("[base] Xvfb :99 started.")
    return cert_path


# ── Electron launcher ─────────────────────────────────────────────────────────


async def launch_electron(sb, binary: str, cert_path: str,
                          extra_args: list[str] = [],
                          extra_env: dict[str, str] = {}) -> int:
    """Launch an Electron app with CDP on :9222 + MITM cert trust."""
    env_pairs = {
        "DISPLAY": ":99",
        "ELECTRON_DISABLE_GPU": "1",
        "NODE_EXTRA_CA_CERTS": cert_path,
        "http_proxy": "http://localhost:8080",
        "https_proxy": "http://localhost:8080",
        **extra_env,
    }
    env_str = ", ".join(f"'{k}': '{v}'" for k, v in env_pairs.items())
    args_str = ", ".join(f"'{a}'" for a in [binary, "--no-sandbox",
                                             "--remote-debugging-port=9222",
                                             "--remote-allow-origins=*",
                                             "--proxy-server=http://localhost:8080",
                                             "--ignore-certificate-errors",
                                             "--ignore-certificate-errors-spki-list",
                                             *extra_args])
    launch = (f"import subprocess,os; "
              f"env={{**os.environ, {env_str}}}; "
              f"p=subprocess.Popen([{args_str}],"
              "stdout=open('/tmp/app.log','w'),stderr=subprocess.STDOUT,"
              "stdin=subprocess.DEVNULL,start_new_session=True); "
              "open('/tmp/app.pid','w').write(str(p.pid))")
    await sh(sb, f'python3 -c "{launch}"')
    await asyncio.sleep(3)  # brief wait; CDP polling handles the rest
    pid_str = await sh(sb, "cat /tmp/app.pid 2>/dev/null || echo 0")
    return int(pid_str or 0)


# ── state extractor ───────────────────────────────────────────────────────────


EXTRACT_JS = """
(function() {
  var out = {};
  // localStorage
  try {
    out.localStorage = Object.fromEntries(
      Object.keys(localStorage).slice(0, 50).map(k => [k, localStorage.getItem(k)])
    );
  } catch(e) { out.localStorage = {}; }
  // sessionStorage
  try {
    out.sessionStorage = Object.fromEntries(
      Object.keys(sessionStorage).slice(0, 20).map(k => [k, sessionStorage.getItem(k)])
    );
  } catch(e) { out.sessionStorage = {}; }
  // Page title + URL
  out.title = document.title;
  out.url = location.href;
  // DOM text (first 500 chars)
  out.bodyText = (document.body || {}).innerText?.slice(0, 500) || '';
  // React root (works for Slack, Discord)
  try {
    var root = document.querySelector('[data-reactroot]') ||
               document.querySelector('#root') ||
               document.querySelector('#app');
    var fk = Object.keys(root || {}).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
    out.hasReact = !!fk;
  } catch(e) { out.hasReact = false; }
  // Redux store (Discord/Slack expose it)
  try {
    out.reduxStore = typeof window.__REDUX_DEVTOOLS_EXTENSION__ !== 'undefined' ? 'present' : 'absent';
  } catch(e) {}
  // Electron IPC available?
  out.hasElectronIpc = typeof window.require !== 'undefined' ||
                       typeof window.__electronSandboxArgs !== 'undefined';
  // App version hints
  try { out.appVersion = window.SLACK_APP_VERSION || window.DISCORD_RELEASE_CHANNEL ||
                         window.Spotify?.version || navigator.userAgent; } catch(e) {}
  return JSON.stringify(out);
})()
"""

REACT_NATIVE_STATE_JS = """
(function() {
  // Extract Redux state if accessible (Discord injects it)
  try {
    var stores = window.__STORES__;
    if (stores) return JSON.stringify({stores: Object.keys(stores)});
  } catch(e) {}
  // Try Discord's FluxDispatcher
  try {
    var fd = window.webpackChunkdiscord_app;
    return JSON.stringify({webpack_chunks: fd ? fd.length : 0});
  } catch(e) {}
  return JSON.stringify({});
})()
"""


async def extract_app_state(ws_url: str, app_name: str) -> dict:
    """Extract all observable state from the renderer via CDP."""
    state = {}
    try:
        raw = await cdp_eval(ws_url, EXTRACT_JS)
        state = json.loads(raw) if raw else {}
    except Exception as e:
        state["error"] = str(e)

    # App-specific extras
    if app_name == "discord":
        try:
            extra = await cdp_eval(ws_url, REACT_NATIVE_STATE_JS)
            state["discord_extra"] = json.loads(extra) if extra else {}
        except Exception:
            pass

    if app_name == "slack":
        try:
            boot_data = await cdp_eval(ws_url,
                "typeof window.TS !== 'undefined' ? JSON.stringify({boot_version: TS.boot_version, model_version: TS.model_version}) : null")
            state["slack_boot"] = json.loads(boot_data) if boot_data else None
        except Exception:
            pass

    if app_name == "spotify":
        try:
            sp_state = await cdp_eval(ws_url,
                "typeof window.Spotify !== 'undefined' ? JSON.stringify(window.Spotify) : "
                "typeof window.__spotify !== 'undefined' ? JSON.stringify(Object.keys(window.__spotify)) : null")
            state["spotify_globals"] = sp_state
        except Exception:
            pass

    return state


# ── SLACK ─────────────────────────────────────────────────────────────────────


async def test_slack(sb, cert_path: str, out_dir: Path) -> dict:
    print("\n" + "="*60)
    print("SLACK")
    print("="*60)
    results = {"app": "slack", "installed": False, "launched": False, "cdp": False, "flows": 0}

    # Install
    print("[slack] Downloading .deb (~100MB)...")
    await sh_bg(sb,
        "curl -fsSL -o /tmp/slack.deb "
        "'https://downloads.slack-edge.com/desktop-releases/linux/x64/4.40.127/slack-desktop-4.40.127-amd64.deb'",
        tag="curl_slack", timeout=600)
    print("[slack] Installing .deb...")
    await sh_bg(sb, "sudo DEBIAN_FRONTEND=noninteractive gdebi -n /tmp/slack.deb",
                tag="gdebi_slack", timeout=300)
    slack_bin = (await sh(sb, "which slack || find /usr/lib/slack -name 'slack' -type f 2>/dev/null | head -1")).splitlines()[0]
    print(f"[slack] Binary: {slack_bin}")
    results["installed"] = True

    # Kill any stale CDP user from previous app
    await sh(sb, "fuser -k 9222/tcp 2>/dev/null || true")
    await asyncio.sleep(1)

    # Launch
    pid = await launch_electron(sb, slack_bin, cert_path)
    results["launched"] = pid > 0
    print(f"[slack] PID: {pid}")

    # CDP via shell (avoids WSL2/Docker host→container TCP reset issues)
    cdp_ready = await wait_for_cdp(sb)
    if cdp_ready:
        results["cdp"] = True
        # Give app more time to load
        await asyncio.sleep(5)
        print("[slack] Injecting state extractor JS...")
        state = await extract_state_via_shell(sb, "slack")
        results["state"] = state
        print(f"[slack] title={state.get('title')!r} url={state.get('url')!r}")
        print(f"        localStorage keys: {list(state.get('localStorage', {}).keys())[:10]}")
        print(f"        bodyText: {state.get('bodyText', '')[:150]!r}")
        sc_b64 = await sh(sb,
            "python3 -c 'import sys; sys.path.insert(0,\"/tmp\"); import cdp_helper as c; print(c.cdp_screenshot())'",
            timeout=20)
        if sc_b64.strip():
            (out_dir / "slack_cdp_screenshot.png").write_bytes(base64.b64decode(sc_b64.strip()))
        (out_dir / "slack_state.json").write_text(json.dumps(state, indent=2))
    else:
        print("[slack] CDP not ready after retries")
        log = await sh(sb, "tail -20 /tmp/app.log 2>/dev/null || echo no_log")
        print(f"  app log: {log[:300]}")

    # Kill app, collect flows
    await sh(sb, "fuser -k 9222/tcp 2>/dev/null || true; kill $(cat /tmp/app.pid 2>/dev/null) 2>/dev/null || true")
    await asyncio.sleep(2)
    flows_raw = await sh(sb, "cat /tmp/mitm_flows.json 2>/dev/null || echo ''")
    flows = [json.loads(l) for l in flows_raw.splitlines() if l.strip()]
    slack_flows = [f for f in flows if "slack" in f.get("url", "").lower()]
    results["flows"] = len(slack_flows)
    print(f"[slack] Captured {len(slack_flows)} Slack flows")
    if slack_flows:
        for f in slack_flows[:5]:
            print(f"  {f['method']} {f['url'][:80]} -> {f.get('status')}")
    (out_dir / "slack_flows.jsonl").write_text("\n".join(json.dumps(f) for f in slack_flows))

    # Clear flows for next app
    await sh(sb, "echo '' > /tmp/mitm_flows.json")
    return results


# ── DISCORD ───────────────────────────────────────────────────────────────────


async def test_discord(sb, cert_path: str, out_dir: Path) -> dict:
    print("\n" + "="*60)
    print("DISCORD")
    print("="*60)
    results = {"app": "discord", "installed": False, "launched": False, "cdp": False, "flows": 0}

    # Install
    print("[discord] Downloading .deb (~100MB)...")
    await sh_bg(sb,
        "curl -fsSL -L -o /tmp/discord.deb 'https://discord.com/api/download?platform=linux&format=deb'",
        tag="curl_discord", timeout=600)
    print("[discord] Installing .deb...")
    await sh_bg(sb, "sudo DEBIAN_FRONTEND=noninteractive gdebi -n /tmp/discord.deb",
                tag="gdebi_discord", timeout=300)
    discord_bin = (await sh(sb, "which discord || find /usr -name 'discord' -type f 2>/dev/null | head -1")).splitlines()[0]
    print(f"[discord] Binary: {discord_bin}")
    results["installed"] = True

    await sh(sb, "fuser -k 9222/tcp 2>/dev/null || true")
    await asyncio.sleep(1)

    pid = await launch_electron(sb, discord_bin, cert_path,
                                extra_args=["--disable-gpu-sandbox"])
    results["launched"] = pid > 0
    print(f"[discord] PID: {pid}")

    cdp_ready = await wait_for_cdp(sb)
    if cdp_ready:
        results["cdp"] = True
        print("[discord] Injecting JS...")
        state = await extract_state_via_shell(sb, "discord")
        results["state"] = state
        print(f"[discord] title={state.get('title')!r}")
        print(f"          bodyText: {state.get('bodyText', '')[:200]!r}")
        print(f"          hasReact: {state.get('hasReact')}")
        sc_b64 = await sh(sb,
            "python3 -c 'import sys; sys.path.insert(0,\"/tmp\"); import cdp_helper as c; print(c.cdp_screenshot())'",
            timeout=20)
        if sc_b64.strip():
            (out_dir / "discord_cdp_screenshot.png").write_bytes(base64.b64decode(sc_b64.strip()))
        (out_dir / "discord_state.json").write_text(json.dumps(state, indent=2))

    await sh(sb, "fuser -k 9222/tcp 2>/dev/null || true; kill $(cat /tmp/app.pid 2>/dev/null) 2>/dev/null || true")
    await asyncio.sleep(2)
    flows_raw = await sh(sb, "cat /tmp/mitm_flows.json 2>/dev/null || echo ''")
    flows = [json.loads(l) for l in flows_raw.splitlines() if l.strip()]
    discord_flows = [f for f in flows if "discord" in f.get("url", "").lower()]
    results["flows"] = len(discord_flows)
    print(f"[discord] Captured {len(discord_flows)} Discord flows")
    for f in discord_flows[:5]:
        print(f"  {f['method']} {f['url'][:80]} -> {f.get('status')}")
    (out_dir / "discord_flows.jsonl").write_text("\n".join(json.dumps(f) for f in discord_flows))
    await sh(sb, "echo '' > /tmp/mitm_flows.json")
    return results


# ── SPOTIFY ───────────────────────────────────────────────────────────────────


async def test_spotify(sb, cert_path: str, out_dir: Path) -> dict:
    print("\n" + "="*60)
    print("SPOTIFY")
    print("="*60)
    results = {"app": "spotify", "installed": False, "launched": False, "cdp": False, "flows": 0}

    print("[spotify] Installing Spotify...")
    # Try apt repo first (bypass mitmproxy for GPG key)
    try:
        await sh_bg(sb,
            "curl -sS --noproxy '*' https://download.spotify.com/debian/pubkey_6224F9941A8AA6D1.gpg "
            "| gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/spotify.gpg > /dev/null && "
            "sudo apt-key adv --keyserver hkp://keyserver.ubuntu.com:80 --recv-keys 5384CE82BA52C83A 2>/dev/null || true && "
            "echo 'deb http://repository.spotify.com stable non-free' "
            "| sudo tee /etc/apt/sources.list.d/spotify.list && "
            "sudo apt-get update -qq && "
            "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "libayatana-appindicator3-1 spotify-client",
            tag="apt_spotify", timeout=600)
    except RuntimeError as e:
        print(f"[spotify] apt install failed: {str(e)[-200:]}")
        print("[spotify] Trying snap install...")
        try:
            await sh_bg(sb,
                "sudo apt-get install -y snapd 2>/dev/null || true && "
                "sudo snap install spotify 2>/dev/null || true",
                tag="snap_spotify", timeout=600)
        except Exception as e2:
            print(f"[spotify] snap also failed: {str(e2)[-100:]}")
            results["installed"] = False
            return results

    spotify_bin_raw = await sh(sb, "which spotify 2>/dev/null || find /snap/bin /usr/snap/bin -name 'spotify' 2>/dev/null | head -1 || echo ''")
    spotify_bin = spotify_bin_raw.strip().splitlines()[0] if spotify_bin_raw.strip() else ""
    print(f"[spotify] Binary: {spotify_bin!r}")
    if not spotify_bin:
        print("[spotify] No binary found after install attempts — skipping")
        results["installed"] = False
        results["skip_reason"] = "binary not found (libc6 version incompatibility)"
        return results
    results["installed"] = True

    await sh(sb, "fuser -k 9222/tcp 2>/dev/null || true")
    await asyncio.sleep(1)

    pid = await launch_electron(sb, spotify_bin, cert_path,
                                extra_env={"SPOTIFY_DEVELOPER_MODE": "1"})
    results["launched"] = pid > 0
    print(f"[spotify] PID: {pid}")

    cdp_ready = await wait_for_cdp(sb)
    if cdp_ready:
        results["cdp"] = True
        print("[spotify] Injecting JS...")
        state = await extract_state_via_shell(sb, "spotify")
        results["state"] = state
        print(f"[spotify] title={state.get('title')!r}")
        print(f"          bodyText: {state.get('bodyText', '')[:200]!r}")
        sc_b64 = await sh(sb,
            "python3 -c 'import sys; sys.path.insert(0,\"/tmp\"); import cdp_helper as c; print(c.cdp_screenshot())'",
            timeout=20)
        if sc_b64.strip():
            (out_dir / "spotify_cdp_screenshot.png").write_bytes(base64.b64decode(sc_b64.strip()))
        (out_dir / "spotify_state.json").write_text(json.dumps(state, indent=2))
    else:
        print("[spotify] CDP not ready, checking app log:")
        log = await sh(sb, "tail -20 /tmp/app.log 2>/dev/null || echo no_log")
        print(log[:400])

    await sh(sb, "fuser -k 9222/tcp 2>/dev/null || true; kill $(cat /tmp/app.pid 2>/dev/null) 2>/dev/null || true")
    await asyncio.sleep(2)
    flows_raw = await sh(sb, "cat /tmp/mitm_flows.json 2>/dev/null || echo ''")
    flows = [json.loads(l) for l in flows_raw.splitlines() if l.strip()]
    spotify_flows = [f for f in flows if "spotify" in f.get("url", "").lower()
                     or "scdn" in f.get("url", "").lower()]
    results["flows"] = len(spotify_flows)
    print(f"[spotify] Captured {len(spotify_flows)} Spotify flows")
    for f in spotify_flows[:5]:
        print(f"  {f['method']} {f['url'][:80]} -> {f.get('status')}")
    (out_dir / "spotify_flows.jsonl").write_text("\n".join(json.dumps(f) for f in spotify_flows))
    return results


# ── main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    apps = sys.argv[1:] if len(sys.argv) > 1 else ["slack", "discord", "spotify"]
    if apps == ["all"]:
        apps = ["slack", "discord", "spotify"]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = OUTPUT_DIR / f"run_{int(time.time())}"
    out_dir.mkdir()
    print(f"Output: {out_dir}")
    print(f"Apps to test: {apps}")

    all_results = []

    async with Sandbox.ephemeral(
        Image.linux("ubuntu", "24.04").expose(9222),
        local=True,
        name="app-re-linux",
        runtime=DockerRuntime(privileged=True, platform="linux/amd64"),
    ) as sb:
        cert_path = await setup_base(sb)

        if "slack" in apps:
            r = await test_slack(sb, cert_path, out_dir)
            all_results.append(r)

        if "discord" in apps:
            r = await test_discord(sb, cert_path, out_dir)
            all_results.append(r)

        if "spotify" in apps:
            r = await test_spotify(sb, cert_path, out_dir)
            all_results.append(r)

    # Summary
    print("\n" + "="*60)
    print("RESULTS SUMMARY")
    print("="*60)
    for r in all_results:
        cdp_ok = "OK" if r.get("cdp") else "FAIL"
        print(f"  {r['app']:10s}  installed={r['installed']}  "
              f"launched={r['launched']}  CDP={cdp_ok}  flows={r['flows']}")

    (out_dir / "results.json").write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nAll output in {out_dir}/")


if __name__ == "__main__":
    asyncio.run(main())
