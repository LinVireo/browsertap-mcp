from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from browsertap_mcp.capture_ownership import CaptureBusyError, CaptureOwnershipRegistry

CLIENT = "chrome:capture-test"
OWNER_A = "capture-requester-a"
OWNER_B = "capture-requester-b"


def command(kind="network_capture", method="start", tab_id=42, **values):
    return {"cmd": kind, "method": method, "tabId": tab_id, **values}


@pytest.mark.parametrize("kind", ["network_capture", "console"])
def test_owner_survives_start_return_but_does_not_block_page_commands(kind):
    registry = CaptureOwnershipRegistry()
    start = registry.prepare(CLIENT, command(kind), OWNER_A)
    assert registry.finish(start, success=True) is False
    assert registry.prepare(CLIENT, {"cmd": "batch", "tabId": 42}, OWNER_B) is None
    assert registry.prepare(CLIENT, command("console", "get"), OWNER_B) is None
    assert registry.prepare(CLIENT, command(kind), OWNER_A) is not None

    for method in ("start", "stop"):
        with pytest.raises(CaptureBusyError) as raised:
            registry.prepare(CLIENT, command(kind, method), OWNER_B)
        assert raised.value.error_code == "capture_busy"
        assert raised.value.retry_safe is True
        assert raised.value.delivery_state == "undelivered"
        assert raised.value.diagnostics == {
            "busy_target": f"{CLIENT}:42",
            "capture_kind": "network" if kind == "network_capture" else "console",
            "may_have_executed": False,
        }
        assert OWNER_A not in str(raised.value)


def test_console_read_is_shared_but_clearing_requires_the_owner():
    registry = CaptureOwnershipRegistry()
    registry.prepare(CLIENT, command("console"), OWNER_A)
    assert registry.prepare(CLIENT, command("console", "get"), None) is None
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command("console", "get", clear=True), OWNER_B)
    clear = registry.prepare(CLIENT, command("console", "get", clear=True), OWNER_A)
    assert registry.finish(clear, success=True) is False
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command("console", "start"), OWNER_B)


@pytest.mark.parametrize("kind", ["network_capture", "console"])
def test_failed_start_or_stop_retains_owner_until_stop_succeeds(kind):
    registry = CaptureOwnershipRegistry()
    start = registry.prepare(CLIENT, command(kind), OWNER_A)
    assert registry.finish(start, success=False) is False
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command(kind), OWNER_B)

    stop = registry.prepare(CLIENT, command(kind, "stop"), OWNER_A)
    assert registry.finish(stop, success=False) is False
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command(kind), OWNER_B)
    assert registry.finish(stop, success=True) is True
    assert registry.prepare(CLIENT, command(kind), OWNER_B) is not None


def test_distinct_capture_kinds_tabs_and_browsers_are_independent():
    registry = CaptureOwnershipRegistry()
    registry.prepare(CLIENT, command(), OWNER_A)
    registry.prepare(CLIENT, command("console"), OWNER_B)
    registry.prepare(CLIENT, command(tab_id=43), OWNER_B)
    registry.prepare("chrome:another-browser", command(), OWNER_B)
    stop = registry.prepare(CLIENT, command(method="stop"), OWNER_A)
    assert registry.finish(stop, success=True) is True
    registry.prepare(CLIENT, command(), OWNER_B)
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command("console"), OWNER_A)


def test_concurrent_first_start_grants_exactly_one_owner():
    registry = CaptureOwnershipRegistry()
    ready = Barrier(8)

    def claim(index):
        requester = f"capture-contender-{index}"
        ready.wait(timeout=5)
        try:
            operation = registry.prepare(CLIENT, command(), requester)
        except CaptureBusyError:
            return None
        return requester, operation

    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(claim, range(8)))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    requester, _operation = winners[0]
    stop = registry.prepare(CLIENT, command(method="stop"), requester)
    assert registry.finish(stop, success=True) is True
    assert registry.prepare(CLIENT, command(), OWNER_B) is not None


def test_confirmed_tab_end_only_releases_that_tabs_capture_kinds():
    registry = CaptureOwnershipRegistry()
    registry.prepare(CLIENT, command(), OWNER_A)
    registry.prepare(CLIENT, command("console"), OWNER_A)
    registry.prepare(CLIENT, command(tab_id=43), OWNER_A)
    assert registry.release_tab(CLIENT, 42) == 2
    assert registry.release_tab(CLIENT, 42) == 0
    registry.prepare(CLIENT, command(), OWNER_B)
    registry.prepare(CLIENT, command("console"), OWNER_B)
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command(tab_id=43), OWNER_B)


def test_confirmed_client_end_keeps_other_browser_owners():
    registry = CaptureOwnershipRegistry()
    registry.prepare(CLIENT, command(), OWNER_A)
    registry.prepare(CLIENT, command("console", tab_id=43), OWNER_A)
    registry.prepare("chrome:another-browser", command(), OWNER_A)
    assert registry.release_client(CLIENT) == 2
    registry.prepare(CLIENT, command(), OWNER_B)
    with pytest.raises(CaptureBusyError):
        registry.prepare("chrome:another-browser", command(), OWNER_B)


def test_delayed_stop_cannot_release_replacement_capture_from_same_requester():
    registry = CaptureOwnershipRegistry()
    registry.prepare(CLIENT, command(), OWNER_A)
    old_stop = registry.prepare(CLIENT, command(method="stop"), OWNER_A)
    registry.release_tab(CLIENT, 42)
    registry.prepare(CLIENT, command(), OWNER_A)
    assert registry.finish(old_stop, success=True) is False
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command(), OWNER_B)


def test_unowned_cleanup_does_not_reserve_or_release_another_capture():
    registry = CaptureOwnershipRegistry()
    unowned_stop = registry.prepare(CLIENT, command(method="stop"), OWNER_A)
    registry.prepare(CLIENT, command(), OWNER_B)
    assert registry.finish(unowned_stop, success=True) is False
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command(), OWNER_A)
    assert registry.finish(None, success=True) is False


@pytest.mark.parametrize("requester", [None, "", "   "])
def test_missing_requester_uses_one_legacy_owner(requester):
    registry = CaptureOwnershipRegistry()
    registry.prepare(CLIENT, command(), None)
    assert registry.prepare(CLIENT, command(), requester) is not None
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command(), OWNER_A)
    stop = registry.prepare(CLIENT, command(method="stop"), requester)
    assert registry.finish(stop, success=True) is True


@pytest.mark.parametrize("requester", [12, False, []])
def test_requester_identity_cannot_be_coerced_from_another_type(requester):
    registry = CaptureOwnershipRegistry()
    with pytest.raises(ValueError, match="requester_id"):
        registry.prepare(CLIENT, command(), requester)


@pytest.mark.parametrize("tab_id", [None, True, 0, -1, 1.5, [], "bad"])
def test_invalid_tab_ids_are_not_registered(tab_id):
    registry = CaptureOwnershipRegistry()
    with pytest.raises(ValueError, match="tabId"):
        registry.prepare(CLIENT, command(tab_id=tab_id), OWNER_A)


def test_numeric_string_tab_id_uses_the_same_ownership_key():
    registry = CaptureOwnershipRegistry()
    registry.prepare(CLIENT, command(tab_id="42"), OWNER_A)
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command(tab_id=42), OWNER_B)


def test_unrelated_commands_do_not_require_capture_identity():
    registry = CaptureOwnershipRegistry()
    assert registry.prepare("", {"cmd": "tabs", "method": "query"}, None) is None
    assert registry.prepare("", {"cmd": "console", "method": "get"}, None) is None
    with pytest.raises(ValueError, match="client_id"):
        registry.prepare("", command(), OWNER_A)


@pytest.mark.parametrize(
    ("kind", "method", "values"),
    [
        ("network_capture", "start", {}),
        ("network_capture", "stop", {}),
        ("console", "start", {}),
        ("console", "stop", {}),
        ("console", "get", {"clear": True}),
    ],
)
def test_proven_dead_owner_allows_takeover_and_retains_new_owner_on_failure(kind, method, values):
    registry = CaptureOwnershipRegistry(
        requester_liveness=lambda requester: "dead" if requester == OWNER_A else "alive",
    )
    registry.prepare(CLIENT, command(kind), OWNER_A)
    old_stop = registry.prepare(CLIENT, command(kind, "stop"), OWNER_A)
    replacement = registry.prepare(CLIENT, command(kind, method, **values), OWNER_B)
    assert replacement is not None
    assert registry.finish(old_stop, success=True) is False
    assert registry.finish(replacement, success=False) is False
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command(kind), "third-requester")
    replacement_stop = registry.prepare(CLIENT, command(kind, "stop"), OWNER_B)
    assert registry.finish(replacement_stop, success=True) is True
    assert registry.prepare(CLIENT, command(kind), "third-requester") is not None


@pytest.mark.parametrize("verdict", ["alive", "unknown", "", "DEAD"])
@pytest.mark.parametrize("method", ["start", "stop", "get"])
def test_only_an_explicit_dead_verdict_allows_another_requester(verdict, method):
    probes = []

    def liveness(requester):
        probes.append(requester)
        return verdict

    registry = CaptureOwnershipRegistry(requester_liveness=liveness)
    registry.prepare(CLIENT, command("console"), OWNER_A)
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command("console", method, clear=True), OWNER_B)
    assert probes == [OWNER_A]
    assert registry.prepare(CLIENT, command("console", method, clear=True), OWNER_A) is not None
    assert probes == [OWNER_A]


def test_liveness_probe_failure_keeps_ownership_and_shared_reads_available():
    def liveness(_requester):
        raise OSError("liveness record unavailable")

    registry = CaptureOwnershipRegistry(requester_liveness=liveness)
    registry.prepare(CLIENT, command("console"), OWNER_A)
    assert registry.prepare(CLIENT, command("console", "get"), OWNER_B) is None
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command("console", "stop"), OWNER_B)


def test_legacy_owner_cannot_be_declared_dead_by_a_liveness_callback():
    registry = CaptureOwnershipRegistry(requester_liveness=lambda _requester: "dead")
    registry.prepare(CLIENT, command(), None)
    with pytest.raises(CaptureBusyError):
        registry.prepare(CLIENT, command(), OWNER_A)


def test_concurrent_recovery_from_dead_owner_grants_exactly_one_new_owner():
    registry = CaptureOwnershipRegistry(
        requester_liveness=lambda requester: "dead" if requester == OWNER_A else "alive",
    )
    registry.prepare(CLIENT, command(), OWNER_A)
    ready = Barrier(8)

    def claim(index):
        requester = f"replacement-contender-{index}"
        ready.wait(timeout=5)
        try:
            registry.prepare(CLIENT, command(), requester)
        except CaptureBusyError:
            return None
        return requester

    with ThreadPoolExecutor(max_workers=8) as executor:
        claims = list(executor.map(claim, range(8)))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    stop = registry.prepare(CLIENT, command(method="stop"), winners[0])
    assert registry.finish(stop, success=True) is True
