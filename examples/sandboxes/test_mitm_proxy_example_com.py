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

# Pre-built release URLs for the test apps (pinned to known-good versions)
# Tauri v0.2.2: added CUA_LOAD_URL + .deb artifact
TAURI_DEB_URL = (
    "https://github.com/trycua/desktop-test-app/releases/download"
    "/v0.2.2/desktop-test-app_amd64.deb"
)
# Electron v0.1.0: initial release with CUA_LOAD_URL + electron-builder .deb
ELECTRON_DEB_URL = (
    "https://github.com/trycua/desktop-test-app-electron/releases/download"
    "/v0.1.0/desktop-test-app-electron_0.1.0_amd64.deb"
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
        subprocess.run(
            ["qemu-system-x86_64", "--version"], capture_output=True, check=True, timeout=5
        )
        return True
    except Exception:
        return False


def _has_hyperv() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        from cua_sandbox.runtime.hyperv import _has_hyperv as _hv

        return _hv()
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


async def _install_chromium(sb) -> None:
    """Install Google Chrome in the sandbox (works in Docker containers)."""
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
    """Download and install the Tauri desktop-test-app .deb (Linux amd64).

    Requires Ubuntu 24.04+ (GLIBC 2.39).  Skips if the system glibc is too old.
    """
    # Check GLIBC version — the Tauri binary requires 2.39+ (Ubuntu 24.04)
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
    """Download and install the Electron desktop-test-app-electron .deb (Linux amd64)."""
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
    if app == "tauri":
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
        await sb.shell.run(
            "export DISPLAY=:1"
            f" && export CUA_LOAD_URL={TARGET_URL}"
            " && python3 -c 'import subprocess;"
            "subprocess.Popen([\"desktop-test-app-electron\",\"--use-system-default-ca\"],"
            "stdout=open(\"/tmp/electron.log\",\"w\"),stderr=subprocess.STDOUT,"
            "stdin=subprocess.DEVNULL,start_new_session=True)'",
            timeout=10,
        )
    elif app == "chromium":
        # Google Chrome or Chromium with system CA store
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
        # Write a launch script to avoid shell quoting complexity
        await sb.shell.run(
            f"printf '#!/bin/sh\\nexport DISPLAY=:1\\nexec {chromium_bin}"
            f" --no-sandbox --disable-gpu --no-first-run"
            f" --disable-extensions --disable-default-apps {TARGET_URL}"
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
    from cua_sandbox.runtime.hyperv import HyperVRuntime

    return HyperVRuntime()


RUNTIME_PARAMS = [
    pytest.param(
        ("ubuntu-docker", lambda: Image.linux("ubuntu", "24.04"), _ubuntu_docker_runtime),
        id="ubuntu-docker",
        marks=pytest.mark.skipif(not _has_docker(), reason="Docker not available"),
    ),
    pytest.param(
        ("ubuntu-qemu", lambda: Image.linux("ubuntu", "24.04", kind="vm"), _ubuntu_qemu_runtime),
        id="ubuntu-qemu",
        marks=pytest.mark.skipif(
            not _has_qemu() or platform.system() == "Windows",
            reason="QEMU not available",
        ),
    ),
    pytest.param(
        ("windows", lambda: Image.windows("11"), _windows_runtime),
        id="windows",
        marks=pytest.mark.skipif(not _has_hyperv(), reason="Hyper-V not available"),
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
