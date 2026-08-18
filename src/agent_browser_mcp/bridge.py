"""Standalone browser bridge daemon and managed lifecycle helpers."""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .browser_bridge import BrowserBridge

logger = logging.getLogger(__name__)
_STATE_DIR_ENV = "AGENT_BROWSER_STATE_DIR"


def bridge_state_dir() -> Path:
    path = Path(os.environ.get(_STATE_DIR_ENV, "")).expanduser() if os.environ.get(_STATE_DIR_ENV) else Path.home() / ".agent-browser-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bridge_pid_path() -> Path:
    return bridge_state_dir() / "bridge.pid"


class ProcessIdentityUnavailable(RuntimeError):
    """The pid may well exist, but this process cannot identify it.

    Deliberately distinct from "no such process". Every caller here uses identity
    to decide whether it may *act* on a pid — delete bridge.pid, report a stop as
    completed, spawn a replacement — and an unqueryable process (owned by another
    user, or elevated) is the one case where guessing "already gone" does real
    damage: ABM throws away the only record of a daemon that is still holding the
    ports, then starts a second one that cannot bind. `physical_input._pid_alive`
    already refuses to read access-denied as death for the same reason.
    """


_ERROR_INVALID_PARAMETER = 87  # how OpenProcess reports a pid that does not exist


def _windows_process_identity(pid: int) -> Optional[dict[str, Any]]:
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    # Declare the handle as HANDLE, not the default c_int: a truncated handle is
    # benign on Windows today but there is no reason to rely on that.
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = kernel32.GetLastError()
        if error == _ERROR_INVALID_PARAMETER:
            return None
        raise ProcessIdentityUnavailable(
            f"OpenProcess failed for pid {pid} with WinError {error}"
        )
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        # The handle opened, so the process exists (or is a zombie Windows still
        # tracks). A failure past this point is "cannot tell", never "gone".
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise ProcessIdentityUnavailable(
                f"GetProcessTimes failed for pid {pid} with WinError "
                f"{kernel32.GetLastError()}"
            )
        size = wintypes.DWORD(32768)
        executable = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, executable, ctypes.byref(size)
        ):
            raise ProcessIdentityUnavailable(
                f"QueryFullProcessImageNameW failed for pid {pid} with WinError "
                f"{kernel32.GetLastError()}"
            )
        creation_ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return {
            "pid": pid,
            "creation_ticks": creation_ticks,
            "executable": os.path.normcase(os.path.abspath(executable.value)),
        }
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_identity(pid: int) -> Optional[dict[str, Any]]:
    proc = Path("/proc") / str(pid)
    try:
        stat = (proc / "stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProcessIdentityUnavailable(f"cannot read {proc}/stat: {exc}") from exc
    try:
        # The executable name in parentheses can contain spaces. Fields after
        # the final ')' start at procfs field 3; starttime is field 22.
        fields = stat[stat.rfind(")") + 2 :].split()
        start_ticks = int(fields[19])
    except (ValueError, IndexError) as exc:
        raise ProcessIdentityUnavailable(f"cannot parse {proc}/stat") from exc
    try:
        executable = str((proc / "exe").resolve(strict=True))
    except FileNotFoundError:
        # The process exited between the two reads; that really is gone.
        return None
    except OSError as exc:
        # /proc/<pid>/exe resolves only for the owner, so another user's daemon
        # is opaque rather than dead.
        raise ProcessIdentityUnavailable(f"cannot resolve {proc}/exe: {exc}") from exc
    return {"pid": pid, "creation_ticks": start_ticks, "executable": executable}


def _darwin_process_identity(pid: int) -> Optional[dict[str, Any]]:
    """macOS identity via ps, because Darwin has no /proc.

    Without this the daemon cannot start on macOS at all: _write_bridge_record
    refuses to run unmanaged, and the /proc reader returns None for every pid.
    Identity is only computed at bridge start/stop and in doctor, never on a hot
    path, so one subprocess is affordable. `lstart` resolves to the second, which
    together with the pid is the same pid-reuse guard the other backends get from
    creation_ticks, just coarser. LC_ALL=C pins the date format.
    """
    env = {**os.environ, "LC_ALL": "C"}
    try:
        completed = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-o", "comm=", "-p", str(pid)],
            capture_output=True, text=True, timeout=10, check=False, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessIdentityUnavailable(f"cannot run ps for pid {pid}: {exc}") from exc
    line = (completed.stdout or "").strip()
    if completed.returncode != 0 or not line:
        # ps exits non-zero and prints nothing precisely when no process matches.
        return None
    parts = line.split(None, 5)
    if len(parts) < 6:
        raise ProcessIdentityUnavailable(f"unexpected ps output for pid {pid}: {line!r}")
    try:
        started = time.strptime(" ".join(parts[:5]), "%a %b %d %H:%M:%S %Y")
    except ValueError as exc:
        raise ProcessIdentityUnavailable(
            f"unparsable ps start time for pid {pid}: {' '.join(parts[:5])!r}"
        ) from exc
    return {
        "pid": pid,
        "creation_ticks": int(time.mktime(started)),
        "executable": os.path.normcase(os.path.abspath(parts[5])),
    }


def process_identity(pid: int) -> Optional[dict[str, Any]]:
    """Identify a pid, or None if it provably does not exist.

    Raises ProcessIdentityUnavailable when the answer is unknowable; callers must
    not fold that into None.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_process_identity(pid)
    if sys.platform == "darwin":
        return _darwin_process_identity(pid)
    return _posix_process_identity(pid)


def _write_bridge_record(*, instance_id: str, host: str, port: int) -> dict[str, Any]:
    try:
        identity = process_identity(os.getpid())
    except ProcessIdentityUnavailable as exc:
        raise RuntimeError(
            f"Cannot identify the bridge process ({exc}); refusing unmanaged startup"
        ) from exc
    if identity is None:
        raise RuntimeError("Cannot identify the bridge process; refusing unmanaged startup")
    record = {
        **identity,
        "instance_id": instance_id,
        "host": host,
        "ws_port": port,
        "http_port": port + 1,
    }
    target = bridge_pid_path()
    fd, temporary = tempfile.mkstemp(prefix="bridge-", suffix=".pid", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
    return record


def read_bridge_record() -> Optional[dict[str, Any]]:
    try:
        value = json.loads(bridge_pid_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    required = {"pid", "creation_ticks", "executable", "instance_id"}
    if not isinstance(value, dict) or not required <= set(value):
        return None
    try:
        value["pid"] = int(value["pid"])
        value["creation_ticks"] = int(value["creation_ticks"])
    except (TypeError, ValueError):
        return None
    return value


def _same_process(record: dict[str, Any], identity: dict[str, Any]) -> bool:
    return (
        identity.get("pid") == record.get("pid")
        and identity.get("creation_ticks") == record.get("creation_ticks")
        and os.path.normcase(str(identity.get("executable", "")))
        == os.path.normcase(str(record.get("executable", "")))
    )


def _remove_record_if_owned(record: dict[str, Any]) -> None:
    current = read_bridge_record()
    if current and current.get("instance_id") == record.get("instance_id"):
        try:
            bridge_pid_path().unlink(missing_ok=True)
        except OSError:
            pass


def _configured_bridge_port_open() -> bool:
    host = os.environ.get("AGENT_BROWSER_TMWD_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_BROWSER_TMWD_PORT", "18765")) + 1
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _process_is_gone(pid: int) -> bool:
    """True only when the pid provably does not exist."""
    try:
        return process_identity(pid) is None
    except ProcessIdentityUnavailable:
        return False


def _terminate_process(pid: int, timeout: float) -> bool:
    if sys.platform == "win32":
        PROCESS_TERMINATE = 0x0001
        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_TERMINATE | SYNCHRONIZE, False, pid
        )
        if not handle:
            # Cannot open it for termination. Only report success if the process
            # is provably gone — an access-denied open (elevated or other-user
            # daemon) must not be reported as a completed stop.
            return _process_is_gone(pid)
        try:
            if not ctypes.windll.kernel32.TerminateProcess(handle, 0):
                return False
            wait_ms = max(0, min(int(timeout * 1000), 0xFFFFFFFE))
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, wait_ms)
            return result == 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except (OSError, PermissionError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _process_is_gone(pid):
            return True
        time.sleep(0.05)
    return False


def stop_bridge_daemon(*, timeout: float = 5.0) -> dict[str, Any]:
    """Stop only the exact managed bridge process recorded in bridge.pid."""
    record = read_bridge_record()
    if record is None:
        if _configured_bridge_port_open():
            return {
                "status": "unmanaged_running",
                "stopped": False,
                "error": (
                    "A process is listening on the bridge port but no verified bridge.pid "
                    "record exists. Refusing to terminate an unknown process; follow the "
                    "legacy bridge migration procedure in docs/TROUBLESHOOTING.md."
                ),
            }
        return {"status": "not_running", "stopped": False}
    try:
        identity = process_identity(record["pid"])
    except ProcessIdentityUnavailable as exc:
        # Do NOT delete bridge.pid here. The recorded process may still be alive
        # and holding the ports; dropping the record would orphan it and let the
        # next --restart spawn a rival daemon that cannot bind.
        return {
            "status": "identity_unavailable",
            "stopped": False,
            "pid": record["pid"],
            "error": (
                f"Cannot determine whether the recorded bridge is still running ({exc}). "
                "bridge.pid was kept. If that pid belongs to an elevated or other-user "
                "bridge, stop it from that account, or follow the legacy bridge migration "
                "procedure in docs/TROUBLESHOOTING.md."
            ),
        }
    if identity is None:
        _remove_record_if_owned(record)
        return {"status": "not_running", "stopped": False, "stale_pid_removed": True}
    if not _same_process(record, identity):
        _remove_record_if_owned(record)
        return {
            "status": "identity_mismatch",
            "stopped": False,
            "stale_pid_removed": True,
            "error": "bridge.pid no longer identifies the recorded process",
        }
    if not _terminate_process(record["pid"], timeout):
        return {
            "status": "stop_failed",
            "stopped": False,
            "error": "The managed bridge process did not stop before the deadline",
        }
    _remove_record_if_owned(record)
    return {"status": "stopped", "stopped": True, "pid": record["pid"]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m agent_browser_mcp.bridge")
    parser.add_argument("--instance-id", default="foreground")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    host = os.environ.get("AGENT_BROWSER_TMWD_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_BROWSER_TMWD_PORT", "18765"))
    driver = BrowserBridge(host=host, port=port)
    record: Optional[dict[str, Any]] = None
    try:
        if driver.is_remote:
            logger.info("Bridge already running at %s:%s; exiting", host, port)
            return 0
        record = _write_bridge_record(
            instance_id=str(args.instance_id), host=host, port=port
        )
        logger.info("Bridge started: ws=%s http=%s pid=%s", port, port + 1, os.getpid())
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Bridge stopped")
        return 0
    finally:
        if record is not None:
            _remove_record_if_owned(record)
        close = getattr(driver, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
