"""Unrelated extensions must not register a second browser or publish tabs."""

import json
from types import SimpleNamespace

import pytest

from browsertap_mcp import extension_origin as O
from tests.test_browser_bridge_coverage import (
    driver_stub,
    ext_ready,
    ws_handler_for,
    ws_peer,
    wsgi_post,
)
from tests.test_browser_bridge_coverage import http_app as http_app


@pytest.fixture(autouse=True)
def clean_origin_environment(monkeypatch):
    monkeypatch.delenv("BROWSERTAP_WS_ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("BROWSERTAP_WS_ALLOW_NO_ORIGIN", raising=False)


@pytest.mark.parametrize("origin", [
    "chrome-extension://" + "a" * 32,
    "chrome-extension://" + "b" * 32,
    "chrome-extension://abc",
    "moz-extension://synthetic-other-install",
    "safari-web-extension://synthetic-other-install",
    "extension://synthetic-other-install",
])
def test_other_extensions_are_not_trusted_by_scheme(origin):
    sock = SimpleNamespace(request=SimpleNamespace(headers={"Origin": origin}))
    assert driver_stub()._origin_allowed(sock) is False


def test_different_client_id_does_not_bypass_the_connection_boundary(monkeypatch):
    driver = driver_stub()
    handler = ws_handler_for(driver, monkeypatch)
    own_origin = "chrome-extension://" + "c" * 32
    monkeypatch.setenv("BROWSERTAP_WS_ALLOWED_ORIGINS", own_origin)
    own = ws_peer(handler, ext_ready("legitimate", tab_id=7), origin=own_origin)
    own.connected()
    own.handle()
    foreign = ws_peer(handler, ext_ready("foreign", tab_id=99),
                      origin="chrome-extension://" + "b" * 32)

    foreign.connected()
    foreign.handle()

    assert foreign.closed is True
    assert list(driver.ext_clients) == ["legitimate"]
    assert list(driver.sessions) == ["legitimate:7"]
    assert driver.select_client_id(use_default=False) == "legitimate"


@pytest.mark.parametrize("suffix", ["/", ".evil.test", "/options.html", "?x=1"])
def test_explicit_origin_is_an_exact_match_not_a_prefix(monkeypatch, suffix):
    own_origin = "chrome-extension://" + "c" * 32
    monkeypatch.setenv("BROWSERTAP_WS_ALLOWED_ORIGINS", own_origin)
    sock = SimpleNamespace(request=SimpleNamespace(headers={"Origin": own_origin + suffix}))
    assert driver_stub()._origin_allowed(sock) is False


@pytest.mark.parametrize("route", ["/link", "/api/result", "/api/longpoll"])
def test_http_origin_filter_also_rejects_other_extensions_and_drains(http_app, route):
    result = wsgi_post(http_app.app, route, {"code": "synthetic" * 20000},
                       origin="chrome-extension://" + "b" * 32)
    assert result["status"] == 403
    assert result["unread_body_bytes"] == 0
    assert http_app.sessions == {}


@pytest.mark.parametrize("windows,expected", [
    (True, "jjlkojfgbeklddcpckipekckcmgcbfjn"),
    (False, "lnkgfdknojmdambfcanadbhmfjfljobb"),
])
def test_unpacked_ids_match_chromiums_public_reference_vectors(windows, expected):
    # components/crx_file/id_util_unittest.cc: GenerateIDForPath.
    assert O.unpacked_extension_id("/path/to/file.ext", windows=windows) == expected


def test_windows_normalizes_only_the_drive_letter():
    assert O.unpacked_extension_id("c:\\BTAP\\extension", windows=True) == (
        O.unpacked_extension_id("C:\\BTAP\\extension", windows=True)
    )
    assert O.unpacked_extension_id("C:\\BTAP\\extension", windows=True) != (
        O.unpacked_extension_id("C:\\btap\\extension", windows=True)
    )


@pytest.mark.parametrize("key", [
    "dGVzdA==",
    "-----BEGIN PUBLIC KEY-----\ndGVzdA==\n-----END PUBLIC KEY-----",
    "-----BEGIN PUBLIC KEY-----\r\ndGVzdA==\r\n-----END PUBLIC KEY-----",
    "-----BEGIN PUBLIC KEY-----\n  dGVz\n\tdA==\n-----END PUBLIC KEY-----",
])
def test_manifest_public_key_takes_precedence_over_unpack_location(tmp_path, key):
    (tmp_path / "manifest.json").write_text(json.dumps({"key": key}), encoding="utf-8")
    # Chromium's GenerateID("test") reference vector. No private key involved.
    assert O._directory_identity(tmp_path) == (
        "chrome-extension://jpignaibiiemhngfjkcpokkamffknabf", "manifest_key",
    )


def test_manifest_without_key_keeps_the_existing_unpack_path_identity(tmp_path):
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    assert O._directory_identity(tmp_path) == (
        "chrome-extension://" + O.unpacked_extension_id(str(tmp_path.resolve())), "unpacked_path",
    )


@pytest.mark.parametrize("manifest", [[], {"key": ""}, {"key": 1}, {"key": None}, {"key": "!bad!"}])
def test_invalid_manifest_does_not_silently_fall_back_to_a_path(tmp_path, manifest):
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        O._directory_identity(tmp_path)


@pytest.mark.parametrize("key", [
    pytest.param("dG\nVz dA==\t", id="raw-ascii-whitespace"),
    pytest.param("dG\u00a0VzdA==", id="raw-unicode-whitespace"),
    pytest.param("dGVzdA==\n", id="raw-trailing-newline"),
    pytest.param("\ndGVzdA==", id="raw-leading-newline"),
    pytest.param("ab", id="missing-two-padding"),
    pytest.param("abc", id="missing-one-padding"),
    pytest.param("abcd=", id="excess-one-padding"),
    pytest.param("abcd====", id="excess-four-padding"),
    pytest.param("====", id="padding-without-data"),
    pytest.param("a" * (100 * 1024 + 4), id="over-chromium-input-limit"),
    pytest.param("\ud800", id="invalid-utf8"),
    pytest.param(
        "-----BEGIN PUBLIC KEY-----\ndG VzdA==\n-----END PUBLIC KEY-----",
        id="pem-inline-space",
    ),
    pytest.param(
        "\n-----BEGIN PUBLIC KEY-----\ndGVzdA==\n-----END PUBLIC KEY-----",
        id="pem-not-at-start",
    ),
    pytest.param("-----BEGIN PUBLIC KEY-----\ndGVzdA==", id="pem-no-footer"),
    pytest.param("-----BEGIN PUBLIC-----\ndGVzdA==\n-----END", id="pem-no-key-marker"),
    pytest.param("-----BEGIN KEY----------END", id="pem-empty-body"),
])
def test_manifest_key_rejects_inputs_chromium_cannot_load(tmp_path, key):
    (tmp_path / "manifest.json").write_text(json.dumps({"key": key}), encoding="utf-8")
    with pytest.raises(ValueError):
        O._directory_identity(tmp_path)


@pytest.mark.parametrize("key", [
    pytest.param("ab==", id="noncanonical-unused-bits-two-padding"),
    pytest.param("abc=", id="noncanonical-unused-bits-one-padding"),
    pytest.param("abcd", id="full-base64-group"),
    pytest.param("a" * (100 * 1024), id="at-chromium-input-limit"),
])
def test_manifest_key_accepts_chromium_strict_base64_boundaries(tmp_path, key):
    (tmp_path / "manifest.json").write_text(json.dumps({"key": key}), encoding="utf-8")
    origin, source = O._directory_identity(tmp_path)
    assert source == "manifest_key"
    assert origin.startswith("chrome-extension://")


def test_unavailable_package_identity_fails_closed_and_reports_the_reason(monkeypatch):
    def unavailable(_directory):
        raise PermissionError("synthetic inaccessible manifest")

    O._packaged_identity.cache_clear()
    try:
        monkeypatch.setattr(O, "_directory_identity", unavailable)
        assert O.default_extension_origin() is None
        assert O.origin_is_allowed("chrome-extension://" + "a" * 32) is False
        assert O.origin_policy_report() == {
            "default_origin": None, "identity_source": "unavailable",
            "additional_origins_count": 0, "allow_no_origin": False,
        }
        explicit = "moz-extension://operator-pinned-install"
        monkeypatch.setenv("BROWSERTAP_WS_ALLOWED_ORIGINS", explicit)
        assert O.origin_is_allowed(explicit) is True
        assert O.origin_is_allowed("") is False
    finally:
        O._packaged_identity.cache_clear()


def test_default_package_identity_accepts_only_the_exact_browser_origin(monkeypatch):
    origin = O.default_extension_origin()
    assert origin is not None
    driver = driver_stub()
    handler = ws_handler_for(driver, monkeypatch)
    own = ws_peer(handler, ext_ready(), origin=origin)
    own.connected()
    own.handle()
    assert own.closed is False
    assert list(driver.sessions) == ["chrome:7"]
    assert O.origin_is_allowed(origin + "/") is False
