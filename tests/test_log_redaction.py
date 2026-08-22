"""The bridge log is a file operators are asked to attach to bug reports.

``bridge.log`` lives for the life of an install and every tab registration used
to write the tab's URL into it verbatim -- so an OAuth callback, a signed
download link or a search query ended up on disk, and "here is my log" handed it
out. Redaction happens at the log call, not in a scrubber afterwards.
"""
from __future__ import annotations

import logging

import pytest

from browsertap_mcp import browser_bridge
from browsertap_mcp.browser_bridge import (
    LOG_URL_PATH_LIMIT,
    redact_pattern,
    redact_url,
)


@pytest.mark.parametrize(
    "url, expected",
    [
        # The case this exists for: an authorization code in the query string.
        (
            "https://id.example.com/oauth2/callback?code=SECRET&state=xyz",
            "https://id.example.com/oauth2/callback?...",
        ),
        # Fragments carry tokens for implicit-flow clients.
        ("https://app.example.com/#access_token=SECRET", "https://app.example.com/#..."),
        # Credentials in the authority are stripped, not truncated.
        ("https://user:pw@example.com/inbox", "https://example.com/inbox"),
        # A plain page keeps everything a reader needs.
        ("https://shop.example.com/cart", "https://shop.example.com/cart"),
        # Local files and inline payloads are content, not location.
        ("file:///D:/Personal/tax-return.pdf", "file:<redacted>"),
        ("data:text/html,<h1>hello", "data:<redacted>"),
        ("blob:https://example.com/9f2c", "blob:<redacted>"),
        ("javascript:alert(document.cookie)", "javascript:<redacted>"),
        # Extension and browser pages identify a tab and hold no secrets.
        ("chrome-extension://abcdefgh/options.html", "chrome-extension://abcdefgh/options.html"),
        ("about:blank", "about:blank"),
        ("chrome://newtab", "chrome://newtab"),
        # Nothing to log, said out loud rather than as an empty column.
        ("", "<no url>"),
        ("   ", "<no url>"),
        (None, "<no url>"),
        (12345, "<no url>"),
        ("/relative/only", "<relative url>"),
    ],
)
def test_redaction_keeps_the_origin_and_drops_the_secrets(url, expected):
    assert redact_url(url) == expected


def test_a_long_path_is_truncated_and_says_so():
    url = "https://example.com/" + "a" * 200
    out = redact_url(url)
    assert out.endswith("...")
    assert len(out) < len(url)
    assert out.startswith("https://example.com/aaa")


def test_a_query_marker_survives_path_truncation():
    # Truncation must not swallow the evidence that a query was removed.
    url = "https://example.com/" + "b" * 200 + "?token=SECRET"
    out = redact_url(url)
    assert "SECRET" not in out
    assert out.endswith("...?...")


def test_the_path_limit_is_configurable_for_callers_that_need_less():
    # The limit is measured over the path as urlsplit hands it over, leading
    # slash included, so 4 leaves "/abc".
    assert redact_url("https://example.com/abcdefghij", path_limit=4) == (
        "https://example.com/abc..."
    )
    assert LOG_URL_PATH_LIMIT > 0


@pytest.mark.parametrize(
    "pattern, expected",
    [
        # The common case: a host or path fragment, which redact_url would
        # flatten to "<relative url>" and so log nothing worth reading.
        ("example.com/cart", "example.com/cart"),
        ("/checkout", "/checkout"),
        # A caller that pasted a whole URL gets the same treatment as a tab URL.
        ("https://id.example.com/cb?code=SECRET", "https://id.example.com/cb?..."),
        ("app.example.com/#access_token=SECRET", "app.example.com/#..."),
        ("", "<no pattern>"),
        (None, "<no pattern>"),
    ],
)
def test_a_search_pattern_is_redacted_without_being_flattened(pattern, expected):
    assert redact_pattern(pattern) == expected


def test_a_long_pattern_is_truncated_and_keeps_the_query_marker():
    out = redact_pattern("example.com/" + "c" * 200 + "?token=SECRET")
    assert "SECRET" not in out
    assert out.endswith("...?...")


def test_an_unmatched_pattern_is_not_logged_verbatim(caplog):
    caplog.set_level(logging.WARNING, logger="browsertap_mcp.browser_bridge")
    bridge = browser_bridge.BrowserBridge.__new__(browser_bridge.BrowserBridge)
    bridge.sessions = {}
    bridge.latest_session_id = None
    bridge.is_remote = False
    assert bridge.set_session(SECRET_URL) is None
    assert "SUPERSECRET" not in caplog.text
    assert "https://id.example.com/callback?..." in caplog.text


SECRET_URL = "https://id.example.com/callback?code=SUPERSECRET&state=zz"


def test_tab_disconnect_logging_does_not_write_the_query_string(caplog):
    caplog.set_level(logging.INFO, logger="browsertap_mcp.browser_bridge")
    session = browser_bridge.Session("chrome:7", {"url": SECRET_URL})
    session.mark_disconnected()
    assert "SUPERSECRET" not in caplog.text
    assert "https://id.example.com/callback?..." in caplog.text


def test_registration_logging_does_not_write_the_query_string(caplog):
    caplog.set_level(logging.INFO, logger="browsertap_mcp.browser_bridge")
    bridge = browser_bridge.BrowserBridge.__new__(browser_bridge.BrowserBridge)
    bridge.sessions = {}
    bridge.default_session_id = None
    bridge.latest_session_id = None
    bridge._register_client("chrome:7", None, {"url": SECRET_URL})
    # Same tab again: the reconnect branch logs too.
    bridge._register_client("chrome:7", None, {"url": SECRET_URL})
    assert "SUPERSECRET" not in caplog.text
    assert caplog.text.count("https://id.example.com/callback?...") == 2


def test_no_log_call_in_the_bridge_passes_a_url_straight_through():
    # A future edit that adds `session.url` to a log line is the regression this
    # test exists to catch; the redaction has to be at every call site.
    from pathlib import Path

    source = Path(browser_bridge.__file__).read_text(encoding="utf-8")
    offenders = []
    for number, line in enumerate(source.splitlines(), start=1):
        if "logger." not in line or "redact_url" in line:
            continue
        if "redact_pattern" in line:
            # A caller's search string, not page data -- redacted by its own
            # helper, which keeps a bare host readable.
            continue
        if any(token in line for token in (".url", "['url']", '["url"]', "url_pattern")):
            offenders.append(f"{number}: {line.strip()}")
    assert offenders == [], "log a redacted URL, not the raw one:\n" + "\n".join(offenders)
