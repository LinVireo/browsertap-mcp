from __future__ import annotations

import argparse
import json
import socket
import sys

from . import __version__
from .server import (
    agent_skills_dir,
    chrome_extension_dir,
    configure_stdio_logging,
    get_driver,
    get_setup_status,
    mcp,
    spawn_bridge_daemon,
)


def cmd_extension_path() -> int:
    path = chrome_extension_dir()
    print(path)
    return 0


def cmd_skill_path() -> int:
    directory = agent_skills_dir()
    print(directory)
    # The names go to stderr so the stdout line stays a single scriptable path,
    # matching `extension-path`, while a human still learns what is in there.
    names = sorted(child.parent.name for child in directory.glob("*/SKILL.md"))
    if names:
        print(f"skills: {', '.join(names)}", file=sys.stderr)
    else:
        print(f"no <name>/SKILL.md found under {directory}", file=sys.stderr)
    return 0


def cmd_print_hermes_config() -> int:
    print(
        "mcp_servers:\n"
        "  browsertap:\n"
        "    command: browsertap\n"
        "    timeout: 120\n"
        "    connect_timeout: 60"
    )
    return 0


def _port_open(host: str, port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(1)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def cmd_doctor() -> int:
    driver = get_driver()
    ws_port = getattr(driver, "port", 18765)
    http_port = ws_port + 1
    sessions = []
    err = None
    diag = None
    try:
        sessions = driver.get_all_sessions()
    except Exception as e:
        err = str(e)
    try:
        diag = driver.diagnose()
    except Exception as e:
        diag = {"cause": "diagnose_failed", "ok": False, "error": str(e)}
    try:
        payload = get_setup_status()
    except Exception as e:
        payload = {
            "status": "bridge_unreachable",
            "action": "restart_bridge",
            "extension_path": str(chrome_extension_dir()),
            "setup_error": str(e),
        }
    payload.update({
        "remote_mode": getattr(driver, "is_remote", False),
        "bridge_host": getattr(driver, "host", "127.0.0.1"),
        "bridge_ws_port": ws_port,
        "bridge_http_port": http_port,
        "ws_port_open": _port_open(getattr(driver, "host", "127.0.0.1"), ws_port),
        "http_port_open": _port_open(getattr(driver, "host", "127.0.0.1"), http_port),
        "connected_tabs": len(sessions),
        "tabs": sessions,
        "diagnosis": payload.get("diagnosis", diag),
        "error": err,
        "next_steps": [
            "Load the unpacked extension in chrome://extensions from extension_path.",
            "Open a normal http/https page in Chrome.",
            "Run your MCP client's connection check for `browsertap-mcp` after adding the config.",
        ],
    })
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    # Surface the one-line verdict last so it's the first thing the eye lands on.
    final_diag = payload.get("diagnosis", diag)
    if payload.get("action") == "restart_mcp_session":
        print(
            "\n[!!] stale_package: A component is newer than this process. Restart the MCP "
            "session/client; restarting the bridge or reloading the extension cannot fix it.",
            file=sys.stderr,
        )
    elif payload.get("action") == "reload_extension":
        print("\n[!!] stale_extension: Reload BrowserTap Bridge once in chrome://extensions.", file=sys.stderr)
    elif payload.get("action") == "restart_bridge":
        print(
            "\n[!!] stale_bridge: Run `browsertap bridge --restart`; "
            "Chrome does not need restarting.",
            file=sys.stderr,
        )
    elif isinstance(final_diag, dict) and final_diag.get("advice"):
        mark = "OK" if final_diag.get("ok") else "!!"
        print(f"\n[{mark}] {final_diag.get('cause')}: {final_diag.get('advice')}", file=sys.stderr)
    return 0 if payload.get("status") == "healthy" else 1


def cmd_bridge(*, stop: bool = False, restart: bool = False) -> int:
    from .bridge import main as bridge_main
    from .bridge import stop_bridge_daemon

    if not stop and not restart:
        return bridge_main([])

    stopped = stop_bridge_daemon()
    if stop:
        print(json.dumps(stopped, ensure_ascii=False, indent=2))
        return 0 if stopped["status"] in {"stopped", "not_running"} else 1

    if stopped["status"] not in {"stopped", "not_running"}:
        payload = {"status": "restart_failed", "stop": stopped, "started": False}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    started = spawn_bridge_daemon(reset_spawn_lock=True)
    payload = {
        "status": "restarted" if started else "restart_failed",
        "stop": stopped,
        "started": started,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if started else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="browsertap",
        description="Real-browser MCP server with a BrowserBridge/CDP transport, screenshots, and physical input.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("extension-path", help="Print the unpacked Chrome extension path")
    sub.add_parser(
        "skill-path",
        help="Print the directory holding the shipped agent skills as <name>/SKILL.md",
    )
    sub.add_parser("doctor", help="Run local diagnostics and print JSON status")
    sub.add_parser("print-hermes-config", help="Print a ready-to-paste Hermes MCP config snippet")
    bridge = sub.add_parser("bridge", help="Run or manage the browser bridge daemon")
    bridge_actions = bridge.add_mutually_exclusive_group()
    bridge_actions.add_argument("--stop", action="store_true", help="Stop the exact managed bridge process")
    bridge_actions.add_argument("--restart", action="store_true", help="Restart the managed bridge in the background")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "extension-path":
        return cmd_extension_path()
    if args.command == "skill-path":
        return cmd_skill_path()
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "print-hermes-config":
        return cmd_print_hermes_config()
    if args.command == "bridge":
        return cmd_bridge(stop=args.stop, restart=args.restart)

    configure_stdio_logging()
    get_driver()
    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
