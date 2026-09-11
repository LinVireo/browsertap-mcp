from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import __version__
from .browser_bridge import tcp_port_open
from .paths import configured_bridge_port
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
    return tcp_port_open(host, port)


def _print_diagnostic(message: str) -> None:
    print(message.encode("ascii", errors="backslashreplace").decode("ascii"), file=sys.stderr)


def cmd_doctor() -> int:
    payload: dict[str, Any]
    try:
        driver = get_driver()
    except Exception as init_error:
        # get_driver() can fail before the bridge is even contacted: invalid host,
        # environment variable type errors, missing dependencies. These failures
        # must still produce JSON so automated tooling can parse the diagnosis.
        payload = {
            "status": "initialization_failed",
            "action": "check_config",
            "extension_path": str(chrome_extension_dir()),
            "error": str(init_error),
            "error_type": type(init_error).__name__,
        }
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        _print_diagnostic(
            f"\n[!!] initialization_failed: {type(init_error).__name__}: {init_error}",
        )
        return 1
    ws_port = getattr(driver, "port", 18765)
    http_port = ws_port + 1
    sessions = []
    err = None
    diag = None
    try:
        payload = get_setup_status()
    except Exception as e:
        # get_setup_status normally contains both the session snapshot and the
        # bridge diagnosis. Only fall back to the direct calls when that status
        # request itself failed; otherwise doctor would pay for both roundtrips
        # twice on every healthy invocation.
        try:
            sessions = driver.get_all_sessions()
        except Exception as session_error:
            err = str(session_error)
        try:
            diag = driver.diagnose()
        except Exception as diagnosis_error:
            diag = {
                "cause": "diagnose_failed",
                "ok": False,
                "error": str(diagnosis_error),
            }
        payload = {
            "status": "bridge_unreachable",
            "action": "restart_bridge",
            "extension_path": str(chrome_extension_dir()),
            "setup_error": str(e),
        }
    else:
        raw_sessions = payload.get("tabs")
        if isinstance(raw_sessions, list):
            sessions = raw_sessions
        else:
            # Keep compatibility with older status providers that predate the
            # detailed tab payload, without adding a call for current bridges.
            try:
                sessions = driver.get_all_sessions()
            except Exception as session_error:
                err = str(session_error)
        raw_diagnosis = payload.get("diagnosis")
        if isinstance(raw_diagnosis, dict):
            diag = raw_diagnosis
        else:
            try:
                diag = driver.diagnose()
            except Exception as diagnosis_error:
                diag = {
                    "cause": "diagnose_failed",
                    "ok": False,
                    "error": str(diagnosis_error),
                }
    host = getattr(driver, "host", "127.0.0.1")
    port_probe_errors: dict[str, Any] = {}
    for field, port in (("ws_port_open", ws_port), ("http_port_open", http_port)):
        try:
            payload[field] = _port_open(host, port)
        except OSError as exc:
            # The setup result is still evidence when a later DNS/socket probe
            # fails. Keep it, and mark only this probe as unknown.
            payload[field] = None
            port_probe_errors[field] = {
                "host": host, "port": port,
                "error": str(exc), "error_type": type(exc).__name__,
            }
    if port_probe_errors:
        payload["port_probe_errors"] = port_probe_errors
    payload.update({
        "remote_mode": getattr(driver, "is_remote", False),
        "bridge_host": host,
        "bridge_ws_port": ws_port,
        "bridge_http_port": http_port,
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
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    # Surface the one-line verdict last so it's the first thing the eye lands on.
    final_diag = payload.get("diagnosis", diag)
    if port_probe_errors:
        print(
            "\n[!!] port_probe_failed: Check bridge_host and local TCP connectivity; "
            "the available setup diagnosis is preserved above.",
            file=sys.stderr,
        )
    elif payload.get("action") == "restart_mcp_session":
        print(
            "\n[!!] stale_package: A component is newer than this process. Restart the MCP "
            "session/client; restarting the bridge or reloading the extension cannot fix it.",
            file=sys.stderr,
        )
    elif payload.get("action") == "reload_extension":
        print("\n[!!] stale_extension: Reload BrowserTap Bridge once in chrome://extensions.", file=sys.stderr)
    elif payload.get("action") == "restart_bridge":
        bridge_status = "stale_bridge" if payload.get("status") == "stale_bridge" else "bridge_unreachable"
        print(
            f"\n[!!] {bridge_status}: Run `browsertap bridge --restart`; "
            "Chrome does not need restarting.",
            file=sys.stderr,
        )
    elif payload.get("action") == "wait_for_extension":
        print(
            "\n[..] starting: the bridge is waiting for the extension handshake; "
            "wait a few seconds and run doctor again.",
            file=sys.stderr,
        )
    elif isinstance(final_diag, dict) and final_diag.get("advice"):
        mark = "OK" if final_diag.get("ok") else "!!"
        _print_diagnostic(f"\n[{mark}] {final_diag.get('cause')}: {final_diag.get('advice')}")
    return 0 if payload.get("status") in {"healthy", "starting"} and not port_probe_errors else 1


def cmd_bridge(*, stop: bool = False, restart: bool = False) -> int:
    from .bridge import main as bridge_main
    from .bridge import stop_bridge_daemon

    try:
        configured_bridge_port()
    except ValueError as exc:
        print(json.dumps({
            "status": "initialization_failed",
            "action": "check_config",
            "error": str(exc),
            "error_type": type(exc).__name__,
        }, ensure_ascii=True, indent=2))
        return 1

    if not stop and not restart:
        return bridge_main([])

    stopped = stop_bridge_daemon()
    if stop:
        print(json.dumps(stopped, ensure_ascii=True, indent=2))
        return 0 if stopped["status"] in {"stopped", "not_running"} else 1

    if stopped["status"] not in {"stopped", "not_running"}:
        payload = {"status": "restart_failed", "stop": stopped, "started": False}
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 1
    started = spawn_bridge_daemon(reset_spawn_lock=True)
    payload = {
        "status": "restarted" if started else "restart_failed",
        "stop": stopped,
        "started": started,
    }
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0 if started else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="browsertap",
        description="Real-browser MCP server with a BrowserBridge/CDP transport, background page input, and screenshots.",
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
