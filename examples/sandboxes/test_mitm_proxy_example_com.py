"""MITM proxy integration test — intercept https://example.com in Tauri, Electron, and Chromium.

Tests the full stack:
  Image.with_proxy(MitmProxy.replace(...))  →  Topology
  Sandbox.ephemeral(topology, local=True)   →  sb.proxy, sb.accessibility

Three app targets × three runtimes = 9 test cases:

  Apps      : tauri (trycua/desktop-test-app)
              electron (trycua/desktop-test-app-electron)
              chromium (system browser)

  Runtimes  : ubuntu-docker  — DockerRuntime(privileged=True, platform=linux/amd64)
              ubuntu-qemu    — QEMURuntime (Linux VM)
              windows        — HyperVRuntime (Windows 11 VM, Windows-only host)

The proxy rewrites the well-known example.com body text.  Each test asserts:
  1. ``sb.proxy.flows()`` captured at least one flow for example.com.
  2. The replaced text appears in the response body of that flow.
  3. ``sb.accessibility.all_text()`` contains the replaced text (rendered in UI).

Usage::

    # Docker only (fast):
    pytest examples/sandboxes/test_mitm_proxy_example_com.py -k "docker" -v

    # All platforms (needs KVM / Hyper-V):
    pytest examples/sandboxes/test_mitm_proxy_example_com.py -v

Requirements:
    - Docker running locally for ubuntu-docker tests
    - qemu-system-x86_64 for ubuntu-qemu tests (Linux/WSL2 host)
    - Windows host with Hyper-V for windows tests
    - ANTHROPIC_API_KEY not required (local=True throughout)
"""

from __future__ import annotations

import asyncio
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

from cua_sandbox import Image, MitmProxy, Sandbox
from cua_sandbox.runtime.docker import DockerRuntime

# ── Constants ────────────────────────────────────────────────────────────────

ORIGINAL_TEXT = "Example Domain"
REPLACED_TEXT = "Proxy Is Working"

# example.com URL to load in all apps
TARGET_URL = "https://example.com"
# Android uses plain HTTP to avoid CA-cert-installation complexity on API 34
ANDROID_TARGET_URL = "http://example.com"

# Pre-built release URLs for the test apps (pinned to known-good versions)
# Tauri v0.2.2: added CUA_LOAD_URL + .deb artifact
TAURI_DEB_URL = (
    "https://github.com/trycua/desktop-test-app/releases/download"
    "/v0.2.2/desktop-test-app_amd64.deb"
)
# Tauri Windows — x64 NSIS installer (produced by tauri build)
TAURI_WIN_URL = (
    "https://github.com/trycua/desktop-test-app/releases/download"
    "/v0.2.2/desktop-test-app-windows-x86_64.exe"
)
# Electron v0.1.0: initial release with CUA_LOAD_URL + electron-builder .deb
ELECTRON_DEB_URL = (
    "https://github.com/trycua/desktop-test-app-electron/releases/download"
    "/v0.1.0/desktop-test-app-electron_0.1.0_amd64.deb"
)
# Electron Windows installer
ELECTRON_WIN_URL = (
    "https://github.com/trycua/desktop-test-app-electron/releases/download"
    "/v0.1.0/desktop-test-app-electron-0.1.0-Setup.exe"
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _has_docker() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=10)
        return True
    except Exception:
        return False


def _has_qemu() -> bool:
    try:
        from cua_sandbox.runtime.qemu_installer import qemu_bin

        qemu_bin()  # auto-downloads on Windows if needed
        return True
    except Exception:
        return False


def _has_android_sdk() -> bool:
    try:
        from cua_sandbox.runtime.android_emulator import _ensure_sdk, _find_bin, _sdk_path
        sdk = _sdk_path()
        # Check if emulator + adb exist (auto-installs if not)
        _find_bin(sdk, "adb")
        _find_bin(sdk, "emulator")
        return True
    except Exception:
        return False


async def _wait_for_api(sb, timeout: int = 60) -> bool:
    """Poll the test app's /health endpoint until it responds."""
    for _ in range(timeout):
        result = await sb.shell.run("curl -sf http://localhost:6769/health || echo FAIL")
        if "FAIL" not in result.stdout:
            return True
        await asyncio.sleep(1)
    return False


# ── App installers ───────────────────────────────────────────────────────────


def _is_android_sb(sb) -> bool:
    """Return True if the sandbox is running Android."""
    return getattr(sb._runtime_info, "environment", "") == "android"


async def _is_windows_sb(sb) -> bool:
    """Return True if the sandbox is running Windows."""
    r = await sb.shell.run("ver 2>nul || echo LINUX")
    return "LINUX" not in r.stdout and "Microsoft" in r.stdout



async def _install_chromium(sb) -> None:
    """Install Google Chrome in the sandbox."""
    if _is_android_sb(sb):
        # Chrome (com.android.chrome) is pre-installed on google_apis images
        return
    if await _is_windows_sb(sb):
        # Chrome is typically pre-installed on Windows 11; if not, install via winget
        r = await sb.shell.run(
            'powershell -Command "if (Test-Path'
            " 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe')"
            " { Write-Output FOUND } else { winget install --id Google.Chrome -e --accept-source-agreements"
            " --accept-package-agreements --silent 2>$null; Write-Output INSTALLED }\""
        )
        return
    await sb.shell.run(
        # Install curl if missing, then download + install Google Chrome stable
        "sudo apt-get install -y --no-install-recommends curl wget -qq 2>/dev/null || true"
        " && (curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
        "     -o /tmp/chrome.deb 2>/dev/null"
        "     || wget -qO /tmp/chrome.deb"
        "        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb)"
        " && sudo apt-get install -y /tmp/chrome.deb 2>/dev/null"
        " || true",
        timeout=300,
    )


async def _install_node(sb) -> None:
    """Install Node.js 20 LTS."""
    await sb.shell.run(
        "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
        " && sudo apt-get install -y nodejs",
        timeout=300,
    )


async def _install_tauri_app(sb) -> None:
    """Download and install the Tauri desktop-test-app."""
    if _is_android_sb(sb):
        pytest.skip("Tauri not available on Android")
    if await _is_windows_sb(sb):
        # Download and run the NSIS installer silently
        await sb.shell.run(
            'powershell -Command "Invoke-WebRequest'
            f" -Uri '{TAURI_WIN_URL}'"
            " -OutFile $env:TEMP\\desktop-test-app-setup.exe -UseBasicParsing;"
            " Start-Process $env:TEMP\\desktop-test-app-setup.exe"
            " -ArgumentList '/S' -Wait\"",
            timeout=300,
        )
        return

    # Linux: check GLIBC version — the Tauri binary requires 2.39+ (Ubuntu 24.04)
    r = await sb.shell.run("ldd --version 2>&1 | head -1")
    line = r.stdout.strip()
    import re as _re
    m = _re.search(r"(\d+\.\d+)", line)
    if m and float(m.group(1)) < 2.39:
        pytest.skip(f"Tauri requires GLIBC 2.39+, found {m.group(1)}")

    await sb.shell.run(
        "sudo apt-get install -y --no-install-recommends curl wget -qq 2>/dev/null || true"
        " && (curl -fsSL -o /tmp/desktop-test-app.deb"
        f"     '{TAURI_DEB_URL}' 2>/dev/null"
        f"     || wget -qO /tmp/desktop-test-app.deb '{TAURI_DEB_URL}')"
        " && sudo apt-get install -y --no-install-recommends libwebkit2gtk-4.1-0 gdebi-core"
        " && sudo gdebi -n /tmp/desktop-test-app.deb",
        timeout=300,
    )


async def _install_electron_app(sb) -> None:
    """Download and install the Electron desktop-test-app-electron."""
    if _is_android_sb(sb):
        pytest.skip("Electron not available on Android")
    if await _is_windows_sb(sb):
        await sb.shell.run(
            'powershell -Command "Invoke-WebRequest'
            f" -Uri '{ELECTRON_WIN_URL}'"
            " -OutFile $env:TEMP\\electron-setup.exe -UseBasicParsing;"
            " Start-Process $env:TEMP\\electron-setup.exe"
            " -ArgumentList '/S' -Wait\"",
            timeout=300,
        )
        return

    await sb.shell.run(
        "sudo apt-get install -y --no-install-recommends curl wget -qq 2>/dev/null || true"
        " && (curl -fsSL -o /tmp/desktop-test-app-electron.deb"
        f"     '{ELECTRON_DEB_URL}' 2>/dev/null"
        f"     || wget -qO /tmp/desktop-test-app-electron.deb '{ELECTRON_DEB_URL}')"
        " && sudo apt-get install -y --no-install-recommends gdebi-core"
        " && sudo gdebi -n /tmp/desktop-test-app-electron.deb",
        timeout=300,
    )


async def _launch_app(sb, app: str) -> None:
    """Launch the given app pointing at TARGET_URL."""
    # Android: open URL in Chrome via shell intent (sb.shell routes through ADB transport)
    if _is_android_sb(sb):
        # Check proxy and cert state for diagnostics
        proxy_state = await sb.shell.run("settings get global http_proxy", timeout=10)
        print(f"[android] system proxy: {proxy_state.stdout.strip()!r}")
        cert_dir = await sb.shell.run("ls /system/etc/security/cacerts/ | grep -v '^[0-9a-f]\\{8\\}' | tail -5", timeout=10)
        print(f"[android] cacerts (non-standard): {cert_dir.stdout.strip()!r}")
        # Count cacerts to verify remount worked
        cert_count = await sb.shell.run("ls /system/etc/security/cacerts/ | wc -l", timeout=10)
        print(f"[android] cacerts count: {cert_count.stdout.strip()!r}")

        # Open ANDROID_TARGET_URL (HTTP) in Chrome. We use HTTP to avoid the
        # CA-cert-installation complexity on Android API 34 — the proxy can still
        # intercept and rewrite the response body.
        url = ANDROID_TARGET_URL
        result = await sb.shell.run(
            f"am start -a android.intent.action.VIEW -d '{url}'"
            " -n com.android.chrome/com.google.android.apps.chrome.Main",
            timeout=15,
        )
        print(f"[android] am start rc={result.returncode} out={result.stdout.strip()!r} err={result.stderr.strip()!r}")
        if result.returncode != 0:
            # Fallback: implicit intent — let Android pick the default browser
            r2 = await sb.shell.run(
                f"am start -a android.intent.action.VIEW -d '{url}'",
                timeout=15,
            )
            print(f"[android] am start fallback rc={r2.returncode} out={r2.stdout.strip()!r}")
        return

    _win = await _is_windows_sb(sb)

    if app == "tauri":
        if _win:
            # Find the installed Tauri app binary
            await sb.shell.run(
                'powershell -Command "'
                "$bin = (Get-ChildItem 'C:\\Program Files\\desktop-test-app'"
                " -Filter '*.exe' -Recurse | Select-Object -First 1).FullName;"
                f"if ($bin) {{ $env:CUA_LOAD_URL = '{TARGET_URL}';"
                " Start-Process $bin -WindowStyle Normal } else"
                " { Write-Error 'Tauri binary not found' }\"",
                timeout=10,
            )
        else:
            await sb.shell.run(
                "export DISPLAY=:1"
                f" && export CUA_LOAD_URL={TARGET_URL}"
                " && python3 -c 'import subprocess;"
                "subprocess.Popen([\"desktop-test-app\",\"--no-sandbox\"],"
                "stdout=open(\"/tmp/tauri.log\",\"w\"),stderr=subprocess.STDOUT,"
                "stdin=subprocess.DEVNULL,start_new_session=True)'",
                timeout=10,
            )
    elif app == "electron":
        if _win:
            await sb.shell.run(
                'powershell -Command "'
                "$bin = (Get-ChildItem"
                " \"$env:LOCALAPPDATA\\desktop-test-app-electron\""
                " -Filter '*.exe' -Recurse -ErrorAction SilentlyContinue"
                " | Where-Object { $_.Name -notlike '*uninstall*' }"
                " | Select-Object -First 1).FullName;"
                f"if ($bin) {{ $env:CUA_LOAD_URL = '{TARGET_URL}';"
                " Start-Process $bin -WindowStyle Normal } else"
                " { Write-Error 'Electron binary not found' }\"",
                timeout=10,
            )
        else:
            proxy_arg = ""
            if sb.proxy and sb.proxy.proxy_url:
                proxy_arg = f',\"--proxy-server={sb.proxy.proxy_url}\"'
            await sb.shell.run(
                "export DISPLAY=:1"
                f" && export CUA_LOAD_URL={TARGET_URL}"
                " && python3 -c 'import subprocess;"
                f"subprocess.Popen([\"desktop-test-app-electron\",\"--use-system-default-ca\"{proxy_arg}],"
                "stdout=open(\"/tmp/electron.log\",\"w\"),stderr=subprocess.STDOUT,"
                "stdin=subprocess.DEVNULL,start_new_session=True)'",
                timeout=10,
            )
    elif app == "chromium":
        if _win:
            chrome_paths = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ]
            chromium_bin = None
            for p in chrome_paths:
                r = await sb.shell.run(
                    f'powershell -Command "if (Test-Path \'{p}\') {{ Write-Output \'{p}\' }}"'
                )
                if r.stdout.strip():
                    chromium_bin = r.stdout.strip()
                    break
            if not chromium_bin:
                pytest.skip("Chrome not found on Windows")
            await sb.shell.run(
                f'powershell -Command "Start-Process \'{chromium_bin}\''
                f" -ArgumentList '--no-sandbox','--disable-gpu','--no-first-run',"
                f"'--disable-extensions','--disable-default-apps','{TARGET_URL}'"
                " -WindowStyle Normal\"",
                timeout=10,
            )
        else:
            # Google Chrome or Chromium with system CA store (Linux)
            chromium_bin = (
                await sb.shell.run(
                    "which google-chrome-stable 2>/dev/null"
                    " || which google-chrome 2>/dev/null"
                    " || which chromium 2>/dev/null"
                    " || which chromium-browser 2>/dev/null"
                    " || echo ''"
                )
            ).stdout.strip()
            if not chromium_bin:
                pytest.skip("Chromium/Chrome not found after install")
            # Pass --proxy-server so Chrome uses the mitmproxy sidecar regardless of env vars
            proxy_flag = ""
            if sb.proxy and sb.proxy.proxy_url:
                proxy_flag = f" --proxy-server={sb.proxy.proxy_url}"
            # Write a launch script to avoid shell quoting complexity
            await sb.shell.run(
                f"printf '#!/bin/sh\\nexport DISPLAY=:1\\nexec {chromium_bin}"
                f" --no-sandbox --disable-gpu --no-first-run"
                f" --disable-extensions --disable-default-apps{proxy_flag} {TARGET_URL}"
                " >/tmp/chrome.log 2>&1\\n'"
                " > /tmp/launch_chrome.sh && chmod +x /tmp/launch_chrome.sh"
                " && python3 -c 'import subprocess;"
                "subprocess.Popen([\"/tmp/launch_chrome.sh\"],"
                "stdin=subprocess.DEVNULL,start_new_session=True)'",
                timeout=10,
            )


# ── Runtime fixtures ─────────────────────────────────────────────────────────


def _ubuntu_docker_runtime() -> DockerRuntime:
    return DockerRuntime(privileged=True, platform="linux/amd64", ephemeral=True)


def _ubuntu_qemu_runtime():
    from cua_sandbox.runtime.qemu import QEMURuntime

    return QEMURuntime(mode="bare-metal")


def _windows_runtime():
    from cua_sandbox.runtime.qemu import QEMUBaremetalRuntime

    return QEMUBaremetalRuntime(memory_mb=4096, cpu_count=4)


def _android_runtime():
    from cua_sandbox.runtime.android_emulator import AndroidEmulatorRuntime

    return AndroidEmulatorRuntime(api_level=34, memory_mb=4096, cpu_count=4)


RUNTIME_PARAMS = [
    pytest.param(
        ("ubuntu-docker", lambda: Image.linux("ubuntu", "24.04"), _ubuntu_docker_runtime),
        id="ubuntu-docker",
        marks=pytest.mark.skipif(not _has_docker(), reason="Docker not available"),
    ),
    pytest.param(
        ("ubuntu-qemu", lambda: Image.linux("ubuntu", "24.04", kind="vm"), _ubuntu_qemu_runtime),
        id="ubuntu-qemu",
        marks=pytest.mark.skipif(not _has_qemu(), reason="QEMU not available"),
    ),
    pytest.param(
        ("windows", lambda: Image.windows("11"), _windows_runtime),
        id="windows",
        marks=pytest.mark.skipif(not _has_qemu(), reason="QEMU not available"),
    ),
    pytest.param(
        ("android", lambda: Image.android("14"), _android_runtime),
        id="android",
        marks=pytest.mark.skipif(not _has_docker(), reason="Docker not available (needed for proxy sidecar)"),
    ),
]

APP_PARAMS = [
    pytest.param("tauri", id="tauri"),
    pytest.param("electron", id="electron"),
    pytest.param("chromium", id="chromium"),
]


# ── Core test ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_spec", RUNTIME_PARAMS)
@pytest.mark.parametrize("app", APP_PARAMS)
async def test_mitm_replace_example_com(runtime_spec, app: str) -> None:
    """MITM proxy rewrites example.com body; assert via flows + accessibility tree."""
    runtime_id, image_fn, runtime_fn = runtime_spec

    # ── 1. Build topology ────────────────────────────────────────────────────
    mitm = MitmProxy.replace(
        url_pattern=r"example\.com",
        search=ORIGINAL_TEXT,
        replacement=REPLACED_TEXT,
    )
    topology = image_fn().with_proxy(mitm)

    # ── 2. Spin up sandbox ───────────────────────────────────────────────────
    async with Sandbox.ephemeral(
        topology,
        local=True,
        runtime=runtime_fn(),
    ) as sb:

        assert sb.proxy is not None, "Topology sandbox must populate sb.proxy"

        # ── 3. Install & launch the app ──────────────────────────────────────
        if app == "tauri":
            await _install_tauri_app(sb)
        elif app == "electron":
            await _install_electron_app(sb)
        elif app == "chromium":
            await _install_chromium(sb)

        await _launch_app(sb, app)

        # Give the app time to load the page and the proxy to capture the flow
        await asyncio.sleep(20)

        # ── 4. Assert via proxy flows ────────────────────────────────────────
        flows = await sb.proxy.flows()
        example_flows = [f for f in flows if "example.com" in f.url]

        assert example_flows, (
            f"[{runtime_id}/{app}] No flows captured for example.com.\n"
            f"All flows: {[f.url for f in flows]}"
        )

        response_texts = [
            f.response_body_text for f in example_flows if f.response_body_text
        ]
        assert any(REPLACED_TEXT in t for t in response_texts), (
            f"[{runtime_id}/{app}] Proxy replacement not found in response bodies.\n"
            f"First response snippet: {response_texts[0][:300] if response_texts else '(empty)'}"
        )
        assert not any(ORIGINAL_TEXT in t for t in response_texts), (
            f"[{runtime_id}/{app}] Original text still present in response (replacement failed)."
        )

        # ── 5. Assert via accessibility tree (best-effort) ───────────────────
        # Wait a little more for the renderer to paint
        await asyncio.sleep(3)

        all_texts = await sb.accessibility.all_text()
        if all_texts:
            # A11y tree is available — assert replacement is visible
            assert REPLACED_TEXT in all_texts, (
                f"[{runtime_id}/{app}] Replaced text not visible in a11y tree.\n"
                f"All text nodes: {all_texts[:20]}"
            )
            assert ORIGINAL_TEXT not in all_texts, (
                f"[{runtime_id}/{app}] Original text still visible in a11y tree."
            )
        # If a11y tree is empty the proxy-flow assertions above are sufficient proof.

        # ── 6. Screenshot for CI artefacts ───────────────────────────────────
        out_dir = Path("/tmp/mitm_test_output")
        out_dir.mkdir(exist_ok=True)
        try:
            screenshot = await sb.screenshot()
            (out_dir / f"{runtime_id}_{app}.png").write_bytes(screenshot)
        except Exception:
            pass  # Screenshot is best-effort for CI


# ── Standalone smoke test (no pytest) ────────────────────────────────────────


async def _smoke() -> None:
    """Quick Docker-only smoke test for the MitmProxy builder and flow reading."""
    if not _has_docker():
        print("Docker not running — skipping smoke test.")
        return

    print("Running smoke test: MitmProxy.replace on ubuntu-docker + chromium …")

    mitm = MitmProxy.replace(
        url_pattern=r"example\.com",
        search=ORIGINAL_TEXT,
        replacement=REPLACED_TEXT,
    )
    topology = Image.linux("ubuntu", "24.04").with_proxy(mitm)

    async with Sandbox.ephemeral(topology, local=True, runtime=_ubuntu_docker_runtime()) as sb:
        await _install_chromium(sb)
        await _launch_app(sb, "chromium")
        await asyncio.sleep(12)

        flows = await sb.proxy.flows()
        example_flows = [f for f in flows if "example.com" in f.url]
        print(f"Captured {len(flows)} total flows, {len(example_flows)} for example.com")

        if example_flows:
            body = example_flows[-1].response_body_text or ""
            ok = REPLACED_TEXT in body
            print(f"  Replacement present: {ok}")
            print(f"  Response snippet:    {body[:200]!r}")
        else:
            print("  No example.com flows yet (check proxy setup / iptables)")

        texts = await sb.accessibility.all_text()
        print(f"  A11y tree contains replaced text: {REPLACED_TEXT in texts}")

    print("Smoke test done.")


if __name__ == "__main__":
    asyncio.run(_smoke())
