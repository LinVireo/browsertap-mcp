"""Traffic refreshes liveness without making a settled transport look new."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from browsertap_mcp import browser_bridge as B
from tests.test_browser_bridge_coverage import FakeSocket, driver_stub, wsgi_post
from tests.test_browser_bridge_coverage import http_app as http_app


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(B.time, "time", lambda: value.now)
    monkeypatch.setattr(B, "HTTP_POLL_SECONDS", 0)
    return value


def poll(driver, *, url="https://example.test/first"):
    response = wsgi_post(driver.app, "/api/longpoll", {
        "sessionId": "synthetic-http", "url": url, "title": "Synthetic tab",
    })
    assert response["status"] == 200
    assert json.loads(response["body"])["ret"] == "next long-poll"
    return driver.sessions["synthetic-http"]


def test_polling_keeps_a_stable_http_session_eligible_for_failover(http_app, clock):
    stable = poll(http_app)
    channel = stable.http_queue
    clock.now += 10
    assert poll(http_app, url="https://example.test/updated") is stable
    fresh = B.Session("synthetic-fresh", {"type": "ws", "url": "https://example.test/new"}, FakeSocket())
    http_app.sessions[fresh.id] = fresh
    http_app.latest_session_id = fresh.id

    assert http_app._pick_failover_session([stable, fresh]) is stable
    assert stable.connect_at == 1000.0
    assert stable.http_queue is channel
    assert stable.url == "https://example.test/updated"


def test_http_liveness_uses_last_poll_and_an_idle_recovery_starts_a_new_age(http_app, clock):
    session = poll(http_app)
    channel = session.http_queue
    for now in (1030.0, 1055.0, 1075.0):
        clock.now = now
        assert poll(http_app) is session
        assert session.is_active()
        assert session.connect_at == 1000.0

    clock.now = 1110.0
    assert session.is_active(), "Old connection age alone must not expire an active poller"
    clock.now = 1140.0
    # No is_active sweep is needed before this poll: the reconnect boundary
    # itself must notice that the prior activity is outside the idle window.
    assert poll(http_app) is session
    assert session.connect_at == 1140.0
    assert session.is_active()
    assert session.http_queue is channel
    clock.now += B.HTTP_SESSION_IDLE_SECONDS + 1
    assert not session.is_active()
    assert session.disconnect_at == clock.now


@pytest.mark.parametrize("transition", ["channel", "transport", "disconnected"])
def test_a_real_reconnect_resets_age_even_when_the_session_id_is_reused(clock, transition):
    channel = FakeSocket()
    session = B.Session("synthetic:1", {"type": "ws", "url": "https://example.test/"}, channel)
    clock.now += 10
    kind = "ws"
    if transition == "channel":
        channel = FakeSocket()
    elif transition == "transport":
        kind = "ext_ws"
    else:
        session.mark_disconnected()
    session.reconnect(channel, {"type": kind, "url": "https://example.test/again"})

    assert session.connect_at == clock.now
    assert session.info["connected_at"] == clock.now
    assert session.is_active()
    assert session.client is channel


def tabs(*, generation="synthetic-generation-1"):
    return [{"id": 7, "url": "https://example.test/", "title": "First", "generation": generation}]


def test_repeated_extension_snapshot_preserves_both_connection_timestamps(clock):
    driver = driver_stub()
    channel = FakeSocket()
    driver._apply_extension_tabs("synthetic", "browser", tabs(), channel)
    session = driver.sessions["synthetic:7"]
    clock.now += 10
    updated = [dict(tabs()[0], title="Updated", url="https://example.test/updated")]
    driver._apply_extension_tabs("synthetic", "browser", updated, channel)

    assert driver.sessions[session.id] is session
    assert session.info["connected_at"] == session.connect_at == 1000.0
    assert session.info["title"] == "Updated"
    assert session.url == "https://example.test/updated"
    assert session.client is channel


@pytest.mark.parametrize("transition", ["channel", "generation", "disconnected"])
def test_extension_transport_or_lifetime_changes_reset_age(clock, transition):
    driver = driver_stub()
    channel = FakeSocket()
    driver._apply_extension_tabs("synthetic", "browser", tabs(), channel)
    original = driver.sessions["synthetic:7"]
    clock.now += 10
    snapshot = tabs()
    if transition == "channel":
        channel = FakeSocket()
    elif transition == "generation":
        snapshot = tabs(generation="synthetic-generation-2")
    else:
        original.mark_disconnected()
    driver._apply_extension_tabs("synthetic", "browser", snapshot, channel)
    current = driver.sessions["synthetic:7"]

    assert current.connect_at == current.info["connected_at"] == clock.now
    assert current.is_active()
    assert current.client is channel
    assert (current is original) is (transition != "generation")
