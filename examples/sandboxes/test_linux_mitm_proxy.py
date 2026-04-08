"""Spin up a Linux XFCE container, install Slack desktop, and intercept all
HTTPS traffic with mitmproxy in transparent mode.

Why Slack for RL training
--------------------------
- Electron app → respects NODE_EXTRA_CA_CERTS env var, so no NSS cert juggling.
- No E2E encryption — all messages are plaintext JSON over HTTPS.
- Rich REST API (accounts, channels, messages, reactions, threads):
    GET  https://slack.com/api/rtm.connect
    POST https://slack.com/api/chat.postMessage
    GET  https://slack.com/api/conversations.history
    GET  https://slack.com/api/users.list
    ...all mockable by returning synthetic JSON from mitmproxy.
- The sign-in screen, channel list, and message feed are all driven by
  interceptable API responses — perfect for constructing controlled RL states.

Architecture
------------
1. DockerRuntime(privileged=True) — needed for iptables inside the container.
2. mitmproxy in transparent mode on port 8080.
3. iptables OUTPUT chain: exempt root (mitmdump), redirect all other 80/443.
4. mitmproxy CA cert installed into system trust store (update-ca-certificates).
5. mitmdump saves flows to /tmp/mitm_flows.bin and streams text to a log.
6. Tuba is launched on DISPLAY=:1 (the container's XFCE desktop).

Usage
-----
    uv run examples/sandboxes/test_linux_mitm_proxy.py

Requirements
------------
    - Docker running locally
"""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

from cua_sandbox import Image, Sandbox
from cua_sandbox.runtime import DockerRuntime

# ── helpers ──────────────────────────────────────────────────────────────────


def _has_docker() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=10)
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


async def _run(sb, cmd: str, check: bool = False, timeout: int = 120) -> str:
    """Run a shell command inside the sandbox and return stdout."""
    result = await sb.shell.run(cmd, timeout=timeout)
    if check and not result.success:
        raise RuntimeError(f"Command failed: {cmd!r}\nstderr: {result.stderr}")
    return result.stdout.strip()


async def _save_screenshot(sb, path: str) -> None:
    data = await sb.screenshot()
    Path(path).write_bytes(data)
    print(f"  [screenshot] saved → {path}")


# ── setup steps ──────────────────────────────────────────────────────────────


async def install_dependencies(sb) -> None:
    """Install mitmproxy and Tuba (GTK4 Mastodon client)."""
    print("\n[1/5] Updating apt and installing dependencies...")

    await _run(sb, "sudo apt-get update -qq", timeout=180)

    await _run(
        sb,
        "sudo apt-get install -y --no-install-recommends "
        "python3-pip pipx ca-certificates curl iptables",
        check=True,
        timeout=300,
    )
    await _run(sb, "pipx install mitmproxy", check=True, timeout=300)
    await _run(sb, "pipx ensurepath")

    print("[1/5] Installing Slack desktop (.deb via gdebi)...")
    # Slack is an Electron app. Electron respects NODE_EXTRA_CA_CERTS, so we
    # don't need to touch NSS — just point that env var at our mitmproxy cert
    # when launching and Slack will trust the intercepted TLS connections.
    # gdebi resolves .deb dependencies properly, unlike plain dpkg -i.
    await _run(
        sb,
        "sudo apt-get install -y --no-install-recommends gdebi-core",
        check=True,
        timeout=120,
    )
    await _run(
        sb,
        "curl -fsSL -o /tmp/slack.deb "
        "'https://downloads.slack-edge.com/desktop-releases/linux/x64/4.40.127/slack-desktop-4.40.127-amd64.deb'",
        check=True,
        timeout=300,
    )
    await _run(
        sb,
        "sudo DEBIAN_FRONTEND=noninteractive gdebi -n /tmp/slack.deb",
        check=True,
        timeout=600,
    )
    print("[1/5] Done.")


async def generate_and_trust_mitm_ca(sb) -> None:
    """Run mitmproxy briefly to generate its CA, then install it system-wide."""
    print("\n[2/5] Generating mitmproxy CA certificate...")

    # Locate mitmdump — pipx installs to ~/.local/bin which may not be on PATH
    # inside a subprocess.Popen call.
    lines = (
        (
            await _run(
                sb, "which mitmdump 2>/dev/null || ls ~/.local/bin/mitmdump 2>/dev/null || echo ''"
            )
        )
        .strip()
        .splitlines()
    )
    mitmdump_bin = lines[0] if lines else ""
    if not mitmdump_bin:
        raise RuntimeError("mitmdump not found after pipx install.")
    print(f"  mitmdump at: {mitmdump_bin}")

    # Run mitmdump briefly to generate ~/.mitmproxy/mitmproxy-ca-cert.pem.
    # Use Python Popen(start_new_session=True) so it doesn't block communicate().
    gen_py = (
        "import subprocess, time; "
        f"p = subprocess.Popen(['{mitmdump_bin}', '--listen-port', '8080'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,"
        " stdin=subprocess.DEVNULL, start_new_session=True); "
        "time.sleep(4); p.terminate(); p.wait()"
    )
    await _run(sb, f'python3 -c "{gen_py}"', timeout=20)

    # Verify cert was generated
    cert_check = await _run(
        sb, "ls -la ~/.mitmproxy/mitmproxy-ca-cert.pem 2>/dev/null || echo MISSING"
    )
    if "MISSING" in cert_check:
        raise RuntimeError("mitmproxy CA cert was not generated. Check mitmdump startup.")
    print(f"  cert: {cert_check.strip()}")

    # Copy into system CA store and update
    await _run(
        sb,
        "sudo cp ~/.mitmproxy/mitmproxy-ca-cert.pem "
        "/usr/local/share/ca-certificates/mitmproxy.crt",
        check=True,
    )
    await _run(sb, "sudo update-ca-certificates", check=True)

    print("[2/5] CA cert trusted system-wide.")


async def start_mitmdump(sb) -> None:
    """Launch mitmdump in transparent mode, writing flows to a log file.

    We use Python's subprocess.Popen(start_new_session=True) inside the
    container to fully detach mitmdump from the computer-server's pipe FDs.
    A plain `cmd &` in bash would keep the pipe open (mitmdump inherits the
    shell's stdout/stderr pipes), causing communicate() to block forever.
    """
    print("\n[3/5] Starting mitmdump (transparent mode, port 8080)...")

    # Find mitmdump — pipx installs to ~/.local/bin but PATH may not include it
    mitmdump_path = await _run(
        sb, "which mitmdump || ls ~/.local/bin/mitmdump 2>/dev/null || echo ''"
    )
    mitmdump_path = mitmdump_path.strip().splitlines()[0] if mitmdump_path.strip() else ""
    if not mitmdump_path:
        raise RuntimeError("mitmdump not found. Check that pipx install mitmproxy succeeded.")
    print(f"  mitmdump binary: {mitmdump_path}")

    # Run mitmdump as root (via sudo) so its outbound connections are owned by
    # UID 0.  The iptables RETURN rule then exempts root's traffic, preventing
    # the redirect loop while still catching all non-root (cua user) traffic.
    launch_py = (
        "import subprocess, sys; "
        "p = subprocess.Popen("
        f"['sudo', '{mitmdump_path}', '--mode', 'transparent', '--listen-port', '8080',"
        " '--set', 'confdir=/home/cua/.mitmproxy',"
        " '--flow-detail', '3', '--save-stream-file', '/tmp/mitm_flows.bin'],"
        " stdout=open('/tmp/mitmdump.log', 'w'), stderr=subprocess.STDOUT,"
        " stdin=subprocess.DEVNULL, start_new_session=True"
        "); "
        "open('/tmp/mitm.pid', 'w').write(str(p.pid)); "
        "print(p.pid)"
    )
    pid_out = await _run(sb, f'python3 -c "{launch_py}"', check=True)
    await asyncio.sleep(3)  # give mitmdump a moment to bind to port 8080

    pid = (await _run(sb, "cat /tmp/mitm.pid 2>/dev/null || echo ''")).strip()
    if not pid:
        log = await _run(sb, "cat /tmp/mitmdump.log 2>/dev/null || echo '(no log)'")
        raise RuntimeError(f"mitmdump failed to start.\n{log}")

    print(f"[3/5] mitmdump running (PID {pid}).")


async def setup_iptables(sb) -> None:
    """Redirect all outbound HTTPS (443) and HTTP (80) through mitmproxy."""
    print("\n[4/5] Configuring iptables transparent redirect...")

    # Get the UID of the current user so we can exempt mitmproxy's own traffic
    uid = (await _run(sb, "id -u")).strip()

    rules = [
        # mitmdump runs as root (UID 0) — exempt root's outbound traffic to
        # prevent the redirect loop (mitmproxy's upstream connections must reach
        # the real server, not be redirected back to port 8080).
        "sudo iptables -t nat -A OUTPUT -m owner --uid-owner 0 -p tcp -j RETURN",
        # Redirect all other outbound 80/443 to mitmproxy
        "sudo iptables -t nat -A OUTPUT -p tcp --dport 80  -j REDIRECT --to-port 8080",
        "sudo iptables -t nat -A OUTPUT -p tcp --dport 443 -j REDIRECT --to-port 8080",
    ]
    for rule in rules:
        await _run(sb, rule, check=True)

    print("[4/5] iptables rules applied.")


async def launch_slack(sb) -> None:
    """Start Slack on the XFCE display.

    NODE_EXTRA_CA_CERTS tells Electron (Node.js) to trust our mitmproxy CA in
    addition to its bundled certs — no NSS cert injection needed.

    On launch Slack hits:
      GET  https://slack.com/api/rtm.connect
      POST https://slack.com/api/auth.signin
      GET  https://slack.com/api/users.info
      GET  https://slack.com/api/conversations.list
      ... all plain JSON over HTTPS.
    """
    print("\n[5/5] Launching Slack on DISPLAY=:1...")

    slack_bin_out = await _run(
        sb,
        "which slack 2>/dev/null || find /usr /opt -name 'slack' -type f 2>/dev/null | head -1 || echo ''",
    )
    slack_bin = slack_bin_out.strip().splitlines()[0] if slack_bin_out.strip() else ""
    if not slack_bin:
        # Show what dpkg actually installed so we can find the binary
        installed = await _run(
            sb, "dpkg -L slack 2>/dev/null | grep -i bin | head -10 || echo '(not found)'"
        )
        pkgs = await _run(sb, "dpkg -l | grep -i slack || echo '(no slack pkg)'")
        raise RuntimeError(
            f"slack binary not found.\ndpkg -l grep:\n{pkgs}\ndpkg files:\n{installed}"
        )
    print(f"  slack binary: {slack_bin}")

    cert_path = "/home/cua/.mitmproxy/mitmproxy-ca-cert.pem"
    launch_py = (
        "import subprocess, os; "
        f"env = {{**os.environ, 'DISPLAY': ':1',"
        f" 'NODE_EXTRA_CA_CERTS': '{cert_path}'}}; "
        "p = subprocess.Popen("
        f"['{slack_bin}', '--no-sandbox'],"
        " env=env,"
        " stdout=open('/tmp/slack.log', 'w'), stderr=subprocess.STDOUT,"
        " stdin=subprocess.DEVNULL, start_new_session=True"
        "); "
        "open('/tmp/slack.pid', 'w').write(str(p.pid))"
    )
    await _run(sb, f'python3 -c "{launch_py}"', check=True)
    await asyncio.sleep(8)  # Electron startup is slower

    pid = (await _run(sb, "cat /tmp/slack.pid 2>/dev/null || echo ''")).strip()
    slack_log = await _run(sb, "head -20 /tmp/slack.log 2>/dev/null || echo ''")
    print(f"[5/5] Slack launched (PID {pid or 'unknown'}).")
    if slack_log.strip():
        print(f"  slack log: {slack_log[:400]}")


# ── main ─────────────────────────────────────────────────────────────────────


async def main() -> None:
    if not _has_docker():
        raise SystemExit("Docker is not running. Start Docker and try again.")

    output_dir = Path("/tmp/mitm_demo")
    output_dir.mkdir(exist_ok=True)

    print("Starting Linux XFCE container with privileged mode (for iptables)...")

    async with Sandbox.ephemeral(
        Image.linux("ubuntu", "24.04"),
        local=True,
        name="mitm-proxy-demo",
        # platform="linux/amd64" runs under Rosetta on Apple Silicon so we can
        # install x86-only binaries like the official Slack .deb.
        runtime=DockerRuntime(privileged=True, platform="linux/amd64"),
    ) as sb:

        # ── 1. Install tooling ────────────────────────────────────────────────
        await install_dependencies(sb)
        await _save_screenshot(sb, str(output_dir / "01_after_install.png"))

        # ── 2. Trust mitmproxy CA ─────────────────────────────────────────────
        await generate_and_trust_mitm_ca(sb)

        # ── 3. Start mitmdump ─────────────────────────────────────────────────
        await start_mitmdump(sb)

        # ── 4. iptables redirect ──────────────────────────────────────────────
        await setup_iptables(sb)

        # Verify rules are in place
        rules_out = await _run(sb, "sudo iptables -t nat -L OUTPUT -n --line-numbers")
        print(f"\niptables OUTPUT (nat):\n{rules_out}\n")

        # ── 5. Launch Slack ───────────────────────────────────────────────────
        await launch_slack(sb)
        await _save_screenshot(sb, str(output_dir / "02_slack_starting.png"))

        # ── 6. Let it run and collect traffic ────────────────────────────────
        print("\nCollecting intercepted traffic for 30 seconds...")
        print("(Slack will hit slack.com/api/rtm.connect, auth, conversations.list, etc.)\n")

        for i in range(3):
            await asyncio.sleep(10)
            elapsed = (i + 1) * 10

            log_tail = await _run(sb, "tail -40 /tmp/mitmdump.log")
            print(f"── mitmdump log @ {elapsed}s ──────────────────────────────")
            print(log_tail or "(no output yet)")
            print()

            await _save_screenshot(sb, str(output_dir / f"03_traffic_{elapsed}s.png"))

        # ── 7. Verify interception: curl a Slack API endpoint ────────────────
        print("\nVerifying interception: curl https://slack.com/api/api.test ...")
        curl_out = await _run(
            sb,
            "curl -s https://slack.com/api/api.test 2>&1 || true",
            timeout=30,
        )
        print(curl_out)

        # ── 8. Export the binary flow file to host ────────────────────────────
        flows_bytes = await _run(sb, "base64 /tmp/mitm_flows.bin 2>/dev/null || echo ''")
        if flows_bytes:
            import base64

            flow_path = output_dir / "mitm_flows.bin"
            flow_path.write_bytes(base64.b64decode(flows_bytes))
            print(f"\nFlow file saved → {flow_path}")
            print("Inspect with:  mitmproxy --rfile /tmp/mitm_demo/mitm_flows.bin")

        final_log = await _run(sb, "cat /tmp/mitmdump.log")
        (output_dir / "mitmdump_full.log").write_text(final_log)
        print(f"Full mitmdump log saved → {output_dir / 'mitmdump_full.log'}")

        await _save_screenshot(sb, str(output_dir / "04_final.png"))

    print(f"\nDone. All output in {output_dir}/")
    print("  01_after_install.png  — desktop after install")
    print("  02_slack_starting.png — Slack launching")
    print("  03_traffic_*.png      — desktop while traffic intercepted")
    print("  04_final.png          — final state")
    print("  mitm_flows.bin        — flow log  (uvx --from mitmproxy mitmdump --rfile ...)")
    print("  mitmdump_full.log     — full text log of intercepted Slack API calls")


if __name__ == "__main__":
    asyncio.run(main())
