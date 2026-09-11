"""Shared fixtures.

The offline layer imports browsertap_mcp directly and never touches a
browser. The live layer needs a running bridge daemon plus the extension
connected, so it is marked `live` and skipped by default (see pyproject).
"""
from __future__ import annotations

import random
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class _GitRepo:
    """A throwaway repository, addressed by the operations the gates need."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def run(self, *args: str) -> str:
        completed = subprocess.run(
            ("git", *args),
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def commit(self, message: str = "change") -> str:
        """Commit everything in the worktree and return the new commit's sha."""
        self.run("add", "-A")
        self.run("commit", "-q", "-m", message)
        return self.head()

    def head(self) -> str:
        return self.run("rev-parse", "HEAD")

    def commit_of(self, ref: str) -> str:
        return self.run("rev-parse", f"{ref}^{{commit}}")


@pytest.fixture
def tagged_repo():
    """Factory for a repository whose single commit is a tagged release.

    Every version and tag gate compares against a release tag, so several test
    modules need the same starting point: one commit, one tag, a real version
    source at the current package path. One builder for all of them, so "what a
    tagged release looks like" cannot drift between the modules that test it.
    """

    def build(root: Path, version: str = "0.3.0", tag: str | None = None) -> _GitRepo:
        repo = _GitRepo(root)
        package = repo.root / "src" / "browsertap_mcp"
        package.mkdir(parents=True, exist_ok=True)
        (package / "_version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        (package / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
        repo.run("init", "-q")
        repo.run("config", "user.email", "btap-test@example.invalid")
        repo.run("config", "user.name", "BTAP Test")
        repo.run("config", "commit.gpgsign", "false")
        repo.commit("release")
        repo.run("tag", tag if tag is not None else f"v{version}")
        return repo

    return build


def _free_port_base() -> int:
    """A port p where p, p+1 and p+2 are all free.

    BrowserBridge takes three consecutive ports (WS, /link, host lock), so a
    single bind test is not enough. Random high ports, retried, rather than
    anything near 18765: a test must never collide with the daemon the user has
    running, let alone bind over it.
    """
    import socket as _s

    for _ in range(50):
        base = random.randint(31000, 60000)
        socks = []
        try:
            for offset in range(3):
                sock = _s.socket()
                # Windows 上普通 bind 会被 SO_REUSEADDR 的双重绑定骗过（探测
                # "成功"，实际端口已被泄漏的旧测试桥占着，请求随机落到旧桥）。
                # 独占探测才回答"这端口真没人用"。
                #
                # POSIX 根本没有这个常量，读属性就抛 AttributeError，
                # 只 catch OSError 拦不住，_free_port_base 本身会爆掉，
                # 所有需要真端口的测试跟着一起错。不设不影响正确性：
                # 没有 SO_REUSEADDR 的 bind 在 POSIX 上本来就是独占的。
                exclusive = getattr(_s, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is not None:
                    try:
                        sock.setsockopt(_s.SOL_SOCKET, exclusive, 1)
                    except OSError:
                        pass
                sock.bind(("127.0.0.1", base + offset))
                socks.append(sock)
        except OSError:
            continue
        finally:
            for sock in socks:
                sock.close()
        if len(socks) == 3:
            return base
    pytest.skip("no free port triple for a test bridge")


class _TestBridge:
    def __init__(self, driver, base):
        self.driver = driver
        self.base = base
        self.port = base + 1     # the /link HTTP port


def _spawn_test_bridge(token, monkeypatch):
    """A real in-process BrowserBridge host on throwaway ports.

    In-process, not a subprocess, so nothing can outlive the test run and start
    competing with the user's daemon for the real ports. Callers must close it
    (see the link_bridge fixtures): a listener left running for the rest of the
    session is what _free_port_base's exclusive probe exists to survive.
    """
    import time

    from browsertap_mcp.browser_bridge import TOKEN_AUTH_ENV, TOKEN_ENV, BrowserBridge

    if token:
        monkeypatch.delenv(TOKEN_AUTH_ENV, raising=False)
        monkeypatch.setenv(TOKEN_ENV, token)
    else:
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        monkeypatch.setenv(TOKEN_AUTH_ENV, "off")
    base = _free_port_base()
    d = BrowserBridge.__new__(BrowserBridge)
    d.host, d.port = "127.0.0.1", base
    d.sessions, d.results, d.acks = {}, {}, {}
    d.default_session_id = d.latest_session_id = None
    d.last_ext_seen = None
    d.client_last_seen, d.ext_clients = {}, {}
    d.is_remote = False
    # Only the HTTP half: these tests are about who may POST to /link, and
    # binding the WS port too would just be another listener to clean up.
    d.start_http_server()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", base + 1)) == 0:
                break
        time.sleep(0.05)
    else:
        import faulthandler

        # An in-process listener that did not start is a failure of this run,
        # not an unavailable optional platform feature. Capture a blocked
        # startup thread before cleanup so CI can show where it stopped.
        faulthandler.dump_traceback()
        d.stop_http_server()
        pytest.fail(f"test bridge never listened on 127.0.0.1:{base + 1}")
    return _TestBridge(d, base)


@pytest.fixture
def link_bridge_auth(monkeypatch):
    """A test bridge that requires a token (the value tests/test_link_auth use)."""
    bridge = _spawn_test_bridge("s3cret-token", monkeypatch)
    try:
        yield bridge
    finally:
        bridge.driver.stop_http_server()


@pytest.fixture
def link_bridge_open(monkeypatch):
    """A test bridge with authentication explicitly disabled."""
    bridge = _spawn_test_bridge(None, monkeypatch)
    try:
        yield bridge
    finally:
        bridge.driver.stop_http_server()


def _tab_inventory(record: dict) -> dict | None:
    """The tab set, read the way the maintainer notes read it by hand.

    Returns None when it cannot be read. A preflight that can break the live
    layer is worse than the manual step it replaces, so an unreadable inventory
    is a recorded note and the run continues without the check.
    """
    from browsertap_mcp import server as S
    from tests import live_preflight as P

    try:
        payload = S.list_all_tabs()
    except Exception as exc:
        record["notes"].append(f"tab inventory unavailable: {type(exc).__name__}: {exc}")
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        record["notes"].append(f"tab inventory unreadable: {str(payload)[:200]}")
        return None
    return P.inventory(data)


def _write_live_preflight(record: dict) -> None:
    """Leave the evidence next to the junit, or say nothing.

    live.yml uploads all of artifacts/, so this is what turns "was the browser
    idle" from something a maintainer remembers into something the run reports.
    """
    import json

    try:
        target = Path(__file__).resolve().parents[1] / "artifacts"
        target.mkdir(parents=True, exist_ok=True)
        (target / "live-preflight.json").write_text(
            json.dumps(record, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:  # never let bookkeeping fail a run
        record["notes"].append(f"could not write live-preflight.json: {exc}")


def _setup_status(record: dict) -> dict | None:
    """What the three processes are each running, or None.

    Same rule as `_tab_inventory`: a precondition that can break the live layer
    is worse than the manual step it replaces, so a status this cannot read is a
    recorded note and the run continues. Being unable to ask is not evidence of
    a skew.
    """
    from browsertap_mcp import server as S

    try:
        status = S.get_setup_status()
    except Exception as exc:
        record["notes"].append(f"component versions unavailable: {type(exc).__name__}: {exc}")
        return None
    if not isinstance(status, dict):
        record["notes"].append(f"component versions unreadable: {str(status)[:200]}")
        return None
    return status


def _owned_tabs(record: dict) -> tuple[list[dict], dict[str, int]] | None:
    """Which tabs this process opened and still owes a close, or None.

    Read from the product's own registry rather than derived from a diff of the
    browser: only the registry knows who opened a tab, and that is the whole
    difference between "the suite leaked one" and "someone was using their
    browser". Same rule as its neighbours -- unreadable is a recorded note, not
    a failure, because being unable to ask is not evidence of a leak.
    """
    from browsertap_mcp import server as S

    try:
        return S._TAB_OWNERSHIP.outstanding(), S._TAB_OWNERSHIP.counters()
    except Exception as exc:
        record["notes"].append(f"tab ownership unreadable: {type(exc).__name__}: {exc}")
        return None


@pytest.fixture(scope="session")
def driver():
    """A BrowserBridge talking to an already-running bridge, or skip.

    Every live test reaches this fixture, directly or through
    `scratch_session`, which makes it the only place that sees the browser
    before the first live test and after the last one. So it is also where the
    live layer's preconditions are enforced. Exactly one of them fails a run:
    the suite must not leave behind a tab it opened. The browser being in use
    and the user's own tabs coming and going are recorded and fail nothing --
    they are the user's to change. `tests/live_preflight.py` holds the
    reasoning; the sampling, the waiting and the verdict live here.
    """
    import time

    from browsertap_mcp.browser_bridge import BrowserBridge
    from tests import live_preflight as P

    d = BrowserBridge()
    try:
        sessions = d.get_all_sessions()
    except Exception as e:  # bridge not up
        pytest.skip(f"bridge unreachable: {e}")
    if not sessions:
        pytest.skip("bridge is up but no tab is connected")

    record: dict = {"notes": []}

    # Before measuring anything about the browser: is this the code under test?
    # Neither of the two long-lived processes is restarted by running pytest, so
    # this is checked first and costs nothing when it passes.
    status = _setup_status(record)
    if status is not None:
        record["components"] = P.component_versions(status)
    stale = P.stale_component_reason(status)
    if stale:
        record["notes"].append(stale.splitlines()[0])
        _write_live_preflight(record)
        # Deliberately a failure and not a skip. A skip is the honest signal for
        # a condition that was absent, and the release chain would eventually
        # reject one anyway -- but only after running everything else, and while
        # naming the wrong problem. See stale_component_reason.
        pytest.fail(stale, pytrace=False)

    # Two samples, not one: an inventory on its own cannot tell an idle browser
    # from a busy one. What that answer is used for is a note -- it makes the
    # timing-sensitive cases noisier, which is worth knowing next to a failure,
    # and is not grounds to withhold the live layer from a browser in use.
    first = _tab_inventory(record)
    time.sleep(P.IDLE_WINDOW_SECONDS)
    baseline = _tab_inventory(record)
    if first is not None and baseline is not None:
        idle = P.compare(first, baseline)
        record["idle_check"] = idle
        record["browser_idle"] = not idle.get("changed")
        note = P.busy_browser_note(idle)
        if note:
            record["notes"].append(note.splitlines()[0])

    try:
        yield d
    finally:
        owned = _owned_tabs(record)
        final = _tab_inventory(record)
        # The user's own tabs, recorded and never judged. Kept because it is the
        # context a reader wants beside a leak -- Chrome discarding one of the
        # suite's tabs shows up here as a re-identification, and that is the
        # difference between a forgotten close_tabs and a refused one.
        if baseline is not None and final is not None:
            record["tab_activity"] = P.compare(baseline, final)
        problem = None
        if owned is not None:
            outstanding, counters = owned
            record["own_tabs"] = {
                "outstanding": outstanding,
                "counters": counters,
                # False means the check had nothing to measure: this process
                # opened no tabs, so "none leaked" is vacuous. Never read a pass
                # here as proof of cleanup without this field.
                "enforced": counters.get("registered", 0) > 0,
            }
            problem = P.leaked_tab_problem(
                outstanding,
                final.values() if final is not None else None,
            )
        _write_live_preflight(record)
        if problem:
            # Teardown, so this cannot be attributed to one test -- which is
            # right: the claim is about the suite, not about any single case.
            raise AssertionError(problem)


@pytest.fixture(scope="session")
def scratch_session(driver):
    """One throwaway tab, reused by every live test, closed at the end.

    Opening a tab per test would litter the user's browser; every live test
    navigates this one instead.
    """
    import time

    from browsertap_mcp import server as S

    result = S.open_new_tab("https://example.com/")
    if result.get("status") == "unknown" or not result.get("owned"):
        pytest.fail(f"scratch tab create was not safely attributable: {result}")
    sid = result["session_id"]
    generation = result["generation"]
    owner_id = result.get("owner_id")

    try:
        registered = False
        for _ in range(20):
            registered = any(
                str(s.get("id")) == sid
                and str(s.get("generation") or "") == str(generation)
                for s in driver.get_all_sessions()
            )
            if registered:
                break
            time.sleep(0.5)
        if not registered:
            pytest.fail(f"scratch tab never registered its exact generation: {result}")

        # server.active_sessions() is cached, and switch_session() looks the target
        # up in that cache. Without this the brand-new tab is "not found" even
        # though the bridge already knows about it.
        S.invalidate_sessions_cache()
        for _ in range(10):
            if any(
                str(s.get("id")) == sid
                and str(s.get("generation") or "") == str(generation)
                for s in S.active_sessions(fresh=True)
            ):
                break
            time.sleep(0.5)

        yield sid
    finally:
        S.close_tabs(sid, session_id=sid, owner_id=owner_id)
