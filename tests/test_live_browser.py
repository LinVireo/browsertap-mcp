"""Live tests: need the bridge daemon running and the extension connected.

Skipped unless you ask for them:  pytest -m live

Everything runs in ONE scratch tab (the scratch_session fixture) so the user's
own tabs are never touched and no tab is left behind. Sites are picked for
different rendering shapes: static HTML, docs with many relative links, a
server-rendered list, and a JS-heavy app.
"""
import base64
import json
import re

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from browsertap_mcp import server as S

pytestmark = pytest.mark.live

STATIC = "https://example.com/"
DOCS = "https://developer.mozilla.org/en-US/docs/Web/API/Window/scrollY"
LIST = "https://news.ycombinator.com/"


def goto(sid, url, selector, timeout=30):
    """Navigate and wait for the page to actually have content."""
    nav = S.open_url(url, session_id=sid, timeout=timeout)
    got = S.wait_for(selector=selector, timeout=timeout, session_id=sid)
    assert got["status"] == "success", f"{url} never rendered {selector}: {got}"
    return nav


class TestScreenshotContent:
    def test_real_capture_returns_mcp_image_content(self, scratch_session):
        goto(scratch_session, STATIC, "h1")

        result = S.capture_page_screenshot(session_id=scratch_session)

        assert isinstance(result, CallToolResult)
        assert isinstance(result.content[0], TextContent)
        assert isinstance(result.content[1], ImageContent)
        assert result.content[1].mimeType == "image/png"
        assert base64.b64decode(result.content[1].data).startswith(b"\x89PNG\r\n\x1a\n")
        assert result.structuredContent["image_attached"] is True


class TestOpenUrl:
    def test_reports_where_it_landed(self, scratch_session):
        nav = goto(scratch_session, STATIC, "h1")
        assert nav["requested_url"] == STATIC
        assert nav["url"]
        assert nav["status"] in ("ok", "redirected")

    def test_landing_url_is_read_back_not_echoed(self, scratch_session):
        """The old code returned the requested URL, so a redirect to a login
        page still came back as status:"ok" with the URL you asked for."""
        nav = goto(scratch_session, STATIC, "h1")
        assert nav.get("title"), "no title read back from the landed page"


class TestDialogPolicy:
    @staticmethod
    def _all_tabs(sid):
        reply = S.list_all_tabs(session_id=sid)
        return reply.get("data") or reply.get("result", {}).get("data", [])

    def test_injected_alert_confirm_and_prompt_are_observable(self, scratch_session):
        goto(scratch_session, STATIC, "h1")

        alert = S.execute_js(
            "alert('BTAP alert'); return 'continued'",
            session_id=scratch_session,
            dialog_policy="dismiss",
            no_monitor=True,
        )
        assert alert["status"] == "ok"
        assert alert["js_return"] == "continued"
        assert alert["dialog"]["type"] == "alert"
        assert alert["dialog"]["message"] == "BTAP alert"

        dismissed = S.execute_js(
            "return confirm('BTAP confirm')",
            session_id=scratch_session,
            dialog_policy="dismiss",
            no_monitor=True,
        )
        assert dismissed["status"] == "ok"
        assert dismissed["js_return"] is False
        assert dismissed["dialog"]["type"] == "confirm"

        accepted = S.execute_js(
            "return confirm('BTAP confirm')",
            session_id=scratch_session,
            dialog_policy="accept",
            no_monitor=True,
        )
        assert accepted["status"] == "ok"
        assert accepted["js_return"] is True

        prompt = S.execute_js(
            "return prompt('BTAP prompt', 'typed text')",
            session_id=scratch_session,
            dialog_policy="accept",
            no_monitor=True,
        )
        assert prompt["status"] == "ok"
        assert prompt["js_return"] == "typed text"
        assert prompt["dialog"]["defaultPrompt"] == "typed text"

    def test_open_url_beforeunload_dismiss_then_accept_and_cleanup(self, scratch_session):
        original_active = next(
            (tab for tab in self._all_tabs(scratch_session) if tab.get("active")), None
        )
        client_id = str(scratch_session).rsplit(":", 1)[0]
        try:
            goto(scratch_session, STATIC, "h1")
            activated = S.activate_tab(scratch_session)
            assert activated["status"] == "ok"
            assert activated["activated_session_id"] == scratch_session
            S.execute_js(
                """
                document.body.insertAdjacentHTML(
                  'beforeend', '<button id="btap-arm-beforeunload">Arm</button>');
                window.__btapBeforeUnload = event => {
                  event.preventDefault();
                  event.returnValue = '';
                };
                document.querySelector('#btap-arm-beforeunload').onclick = () => {
                  addEventListener('beforeunload', window.__btapBeforeUnload);
                };
                return true;
                """,
                session_id=scratch_session,
                no_monitor=True,
            )
            S.page_click(selector="#btap-arm-beforeunload", session_id=scratch_session)

            dismissed = S.open_url(
                "https://example.org/",
                session_id=scratch_session,
                beforeunload="dismiss",
                timeout=20,
            )
            assert dismissed["status"] == "blocked_by_beforeunload"
            assert "example.com" in dismissed["url"]
            assert dismissed["dialog"]["type"] == "beforeunload"

            accepted = S.open_url(
                "https://example.org/",
                session_id=scratch_session,
                beforeunload="accept",
                timeout=20,
            )
            assert accepted["status"] == "ok"
            S.wait_for(url_pattern="example.org", session_id=scratch_session, timeout=20)
        finally:
            try:
                S.execute_js(
                    "removeEventListener('beforeunload', window.__btapBeforeUnload); return true",
                    session_id=scratch_session,
                    no_monitor=True,
                )
            except Exception:
                pass
            if original_active is not None:
                S.require_driver().ext_cmd(
                    {"cmd": "tabs", "method": "switch", "tabId": original_active["id"]},
                    client_id=client_id,
                    timeout=15.0,
                )


class TestWaitFor:
    def test_selector_present(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(selector="h1", timeout=10, session_id=scratch_session)
        assert r["status"] == "success"
        assert r["waited_ms"] is not None

    def test_text_present(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(text="Example Domain", timeout=10, session_id=scratch_session)
        assert r["status"] == "success"

    def test_url_pattern(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(url_pattern=r"example\.com", timeout=10,
                       session_id=scratch_session)
        assert r["status"] == "success"

    def test_js_expression(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(js="document.readyState === 'complete'", timeout=10,
                       session_id=scratch_session)
        assert r["status"] == "success"

    def test_absent_selector_times_out(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(selector="#definitely-not-here-xyz", timeout=3,
                       session_id=scratch_session)
        assert r["status"] == "timeout"
        # It must actually have waited, not returned instantly.
        assert r["waited_ms"] >= 2500, r["waited_ms"]

    def test_gone_on_a_permanent_element_times_out(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for(selector="body", gone=True, timeout=3,
                       session_id=scratch_session)
        assert r["status"] == "timeout"

    def test_a_long_wait_is_still_one_roundtrip(self, scratch_session):
        """Polling happens in-page, so a 5s wait must not cost 50 bridge calls.
        Measured indirectly: the call returns close to the timeout, not much
        later, which it would if each poll were a roundtrip."""
        import time

        goto(scratch_session, STATIC, "h1")
        t0 = time.time()
        r = S.wait_for(selector="#nope-xyz", timeout=4, session_id=scratch_session)
        elapsed = time.time() - t0
        assert r["status"] == "timeout"
        assert elapsed < 12, f"took {elapsed:.1f}s for a 4s in-page wait"


class TestScrollPage:
    def test_bottom_then_top(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        bottom = S.scroll_page(to="bottom", session_id=scratch_session, timeout=20)
        assert isinstance(bottom["scroll_y"], int)
        assert bottom["doc_height"] > 0
        if bottom["doc_height"] > bottom["viewport_height"] + 100:
            assert bottom["moved"] or bottom["at_bottom"]
        top = S.scroll_page(to="top", session_id=scratch_session, timeout=20)
        assert top["scroll_y"] == 0

    def test_pixel_offset(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        S.scroll_page(to="top", session_id=scratch_session, timeout=20)
        r = S.scroll_page(to="300", session_id=scratch_session, timeout=20)
        assert r["scroll_y"] > 0

    def test_selector_scrolls_into_view(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        r = S.scroll_page(to="footer, h1", session_id=scratch_session, timeout=20)
        assert r["status"] == "success"

    def test_missing_selector_does_not_raise(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.scroll_page(to="#not-a-real-element", session_id=scratch_session,
                          timeout=20)
        assert r["status"] == "not_found"
        assert r["selector"] == "#not-a-real-element"
        assert "did not match" in r["note"]

    def test_scroll_page_cleanup_restores_top(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        try:
            S.scroll_page(to="bottom", session_id=scratch_session, timeout=20)
        finally:
            restored = S.scroll_page(to="top", session_id=scratch_session, timeout=20)
        assert restored["scroll_y"] == 0


class TestScanPageLinks:
    def test_refs_replace_long_hrefs(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        assert scan["status"] == "success"
        assert "__link__" not in scan["content"]

    def test_every_ref_resolves(self, scratch_session):
        goto(scratch_session, LIST, "td.title, .titleline")
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        assert scan["status"] == "success"
        used = set(re.findall(r'href="#(r\d+)"', scan["content"]))
        assert used <= set(scan.get("links", {})), "unresolvable refs in content"

    def test_refs_are_absolute_urls(self, scratch_session):
        """MDN serves relative hrefs; a ref of '/en-US/docs/x' is useless to
        open_url, so they must be resolved against the page URL."""
        goto(scratch_session, DOCS, "h1")
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        links = scan.get("links", {})
        assert links, "docs page produced no link refs"
        relative = [u for u in links.values() if not u.startswith(("http://", "https://"))]
        assert not relative, f"relative refs survived: {relative[:5]}"

    def test_a_ref_can_actually_be_navigated(self, scratch_session):
        """End to end: read a link off the page, then go there."""
        goto(scratch_session, DOCS, "h1")
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        target = next((u for u in scan.get("links", {}).values()
                       if u.startswith("https://developer.mozilla.org")), None)
        if not target:
            pytest.skip("no mozilla link to follow on this render")
        nav = S.open_url(target, session_id=scratch_session, timeout=30)
        assert nav["status"] in ("ok", "redirected")
        assert nav["url"]


class TestScanPageOffscreen:
    def test_long_page_reports_dropped_elements(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        S.scroll_page(to="top", session_id=scratch_session, timeout=20)
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        assert scan["status"] == "success"
        off = scan.get("offscreen")
        if off is None:
            pytest.skip("page fit inside the clamp on this viewport")
        assert off["elements"] > 0
        assert off["doc_height"] >= off["viewport_height"]
        assert scan.get("hint"), "offscreen elements but no hint for the agent"

    def test_marker_is_not_left_in_the_content(self, scratch_session):
        goto(scratch_session, DOCS, "h1")
        scan = S.scan_page(session_id=scratch_session, maxchars=60000, timeout=30)
        # The base-url marker is an internal detail and must not reach the agent.
        assert "btap-base:" not in scan["content"]


class TestNavigationOutcome:
    def test_click_that_navigates_is_not_reported_as_success(self, scratch_session):
        """The core regression: a click that unloads the page used to come back
        as status:"success" with a diagnostic string as the return value."""
        goto(scratch_session, STATIC, "h1")
        rr = S.execute_js(
            "var a=document.createElement('a');"
            "a.href='https://example.org/';document.body.appendChild(a);"
            "a.click();return 'never-seen'",
            session_id=scratch_session, no_monitor=True, timeout=15)
        if rr["status"] == "navigated":
            assert rr.get("js_return") is None
            assert rr.get("js_return_lost")
        else:
            # Fast pages can return before unload; that is a genuine success,
            # but the return value must be the script's, never a bridge string.
            assert rr["status"] == "success"
            assert "reloaded" not in str(rr.get("js_return"))

    def test_execute_js_plain_return_still_works(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        rr = S.execute_js("return 1+1", session_id=scratch_session,
                          no_monitor=True, timeout=15)
        assert rr["status"] == "success"
        assert rr["js_return"] == 2


class TestExplicitSessionPipeline:
    def test_monitored_execute_stays_on_explicit_session(self, scratch_session):
        """Every monitor/readback roundtrip must remain pinned to the named tab."""
        server_driver = S.require_driver()
        created = S.open_new_tab(
            "https://example.com/?btap-explicit-other=211",
            timeout=20,
            active=False,
            session_id=scratch_session,
        )
        other_session = created.get("session_id")
        previous_default = server_driver.default_session_id
        marker = "btap-explicit-session-211"
        try:
            assert created["status"] == "ok"
            assert created["owned"] is True
            assert other_session
            S.wait_for(selector="h1", timeout=20, session_id=other_session)
            goto(scratch_session, STATIC, "h1")
            server_driver.default_session_id = other_session
            result = S.execute_js(
                f"document.body.dataset.btapExplicitTarget = '{marker}'; return document.title",
                session_id=scratch_session,
                no_monitor=False,
                timeout=15,
            )
            assert result["status"] in ("ok", "success")
            assert str(result["tab_id"]) == str(scratch_session).rsplit(":", 1)[-1]
            assert server_driver.default_session_id == other_session

            scratch_marker = S.execute_js(
                "return document.body.dataset.btapExplicitTarget || null",
                session_id=scratch_session,
                no_monitor=True,
                timeout=10,
            )
            other_marker = S.execute_js(
                "return document.body.dataset.btapExplicitTarget || null",
                session_id=other_session,
                no_monitor=True,
                timeout=10,
            )
            assert scratch_marker["js_return"] == marker
            assert other_marker["js_return"] is None
            assert server_driver.default_session_id == other_session
        finally:
            server_driver.default_session_id = previous_default
            if other_session:
                S.close_tabs(other_session, owner_id=created.get("owner_id"))


class TestNewTabGeneration:
    def test_open_new_tab_returns_matching_live_generation(self, scratch_session):
        created = S.open_new_tab(
            "https://example.com/?btap-generation=211",
            timeout=20,
            active=False,
            session_id=scratch_session,
        )
        created_sid = created.get("session_id")
        try:
            assert created["status"] == "ok"
            assert created["ready"] is True
            assert created["generation"]
            assert created_sid
            matching = next(
                tab for tab in S.compact_tabs(fresh=True)
                if tab["id"] == created_sid
            )
            assert str(matching["generation"]) == str(created["generation"])
            immediate = S.execute_js(
                "return location.href",
                session_id=created_sid,
                no_monitor=True,
                timeout=15,
            )
            assert immediate["status"] in ("ok", "success")
            assert "btap-generation=211" in immediate["js_return"]
        finally:
            if created_sid:
                S.close_tabs(created_sid, owner_id=created.get("owner_id"))


class TestBackgroundPageInput:
    def test_never_activated_tab_receives_background_input(self, scratch_session):
        original_active = next(
            (tab for tab in S.list_all_tabs(session_id=scratch_session).get("data", [])
             if tab.get("active")),
            None,
        )
        created = S.open_new_tab(
            "https://example.com/?btap-never-activated-input=1",
            timeout=20,
            active=False,
            session_id=scratch_session,
        )
        background_sid = created.get("session_id")
        try:
            assert created["status"] == "ok"
            assert created["owned"] is True
            assert background_sid
            S.wait_for(selector="h1", timeout=20, session_id=background_sid)
            S.execute_js(
                """
                document.body.innerHTML = '<button id="never-active-button">Click</button>';
                window.__neverActiveEvents = [];
                document.addEventListener('click', event => {
                  window.__neverActiveEvents.push({type: event.type, id: event.target.id});
                }, true);
                return true;
                """,
                session_id=background_sid,
                no_monitor=True,
                timeout=15,
            )

            clicked = S.page_click(
                selector="#never-active-button",
                session_id=background_sid,
                timeout=15,
            )
            observed = S.execute_js(
                "return JSON.stringify(window.__neverActiveEvents)",
                session_id=background_sid,
                no_monitor=True,
                timeout=15,
            )

            assert clicked["status"] == "success"
            assert clicked["foreground_changed"] is False
            assert json.loads(observed["js_return"]) == [
                {"type": "click", "id": "never-active-button"}
            ]
            if original_active is not None:
                tabs = S.list_all_tabs(session_id=scratch_session).get("data", [])
                assert next(
                    tab.get("active") for tab in tabs
                    if tab.get("id") == original_active["id"]
                ) is True
        finally:
            if background_sid:
                S.close_tabs(background_sid, owner_id=created.get("owner_id"))

    def test_page_click_structured_locators_execute_against_real_dom(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        setup = S.execute_js(
            """
            document.body.innerHTML = `
              <button id="role-target" aria-label="Role target">role</button>
              <button id="text-target">Text target</button>
              <label for="label-target">Email address</label>
              <input id="label-target">
              <button id="disabled-target" disabled>Disabled target</button>
              <button id="aria-disabled-target" aria-disabled="true">ARIA disabled</button>
              <button class="duplicate">Duplicate target</button>
              <button class="duplicate">Duplicate target</button>
              <div id="open-host"></div>
              <div id="closed-host"></div>
              <iframe id="opaque-frame" sandbox srcdoc="<button>Opaque</button>"></iframe>
            `;
            window.__locatorEvents = [];
            const record = value => window.__locatorEvents.push(value);
            document.querySelector('#role-target').addEventListener('click', () => record('role'));
            document.querySelector('#text-target').addEventListener('click', () => record('text'));
            const openRoot = document.querySelector('#open-host').attachShadow({mode: 'open'});
            openRoot.innerHTML = `
              <button id="shadow-button" aria-label="Shadow target">shadow</button>
              <label for="shadow-input">Shadow field</label>
              <input id="shadow-input">
            `;
            openRoot.querySelector('#shadow-button').addEventListener('click', () => record('shadow'));
            document.querySelector('#closed-host').attachShadow({mode: 'closed'});

            const outer = document.createElement('iframe');
            outer.id = 'outer-frame';
            outer.style.cssText = 'display:block;margin:80px 0 0 260px;width:520px;height:260px;border:12px solid black';
            document.body.appendChild(outer);
            return new Promise((resolve, reject) => {
              const timer = setTimeout(() => reject(new Error('locator iframe setup timed out')), 5000);
              outer.onload = () => {
                const outerDoc = outer.contentDocument;
                outerDoc.body.style.margin = '0';
                const inner = outerDoc.createElement('iframe');
                inner.id = 'inner-frame';
                inner.style.cssText = 'display:block;margin:55px 0 0 140px;width:260px;height:120px;border:8px solid blue';
                inner.onload = () => {
                  clearTimeout(timer);
                  const innerDoc = inner.contentDocument;
                  innerDoc.body.innerHTML = '<button id="deep-button" aria-label="Deep target" style="margin:24px;width:120px;height:44px">deep</button>';
                  innerDoc.querySelector('#deep-button').addEventListener('click', () => record('deep'));
                  resolve(JSON.stringify({ready: true}));
                };
                outerDoc.body.appendChild(inner);
                inner.src = 'about:blank';
              };
              outer.src = 'about:blank';
            });
            """,
            session_id=scratch_session,
            no_monitor=True,
            timeout=15,
        )
        assert json.loads(setup["js_return"]) == {"ready": True}

        assert S.page_click(
            selector={"role": "button", "name": "Role target", "exact": True},
            session_id=scratch_session,
        )["status"] == "success"
        assert S.page_click(
            selector={"text": "Text target", "exact": True},
            session_id=scratch_session,
        )["status"] == "success"
        assert S.page_click(
            selector={
                "role": "button",
                "name": "Deep target",
                "exact": True,
                "frame": [{"css": "#outer-frame"}, {"css": "#inner-frame"}],
            },
            session_id=scratch_session,
        )["status"] == "success"
        assert S.page_click(
            selector={
                "role": "button",
                "name": "Shadow target",
                "exact": True,
                "shadow": ["#open-host"],
            },
            session_id=scratch_session,
        )["status"] == "success"

        typed = S.page_type(
            "top-level",
            selector={"label": "Email address"},
            clear=True,
            session_id=scratch_session,
        )
        shadow_typed = S.page_type(
            "inside-shadow",
            selector={"label": "Shadow field", "shadow": ["#open-host"]},
            clear=True,
            session_id=scratch_session,
        )
        assert typed["status"] == shadow_typed["status"] == "success"

        failures = {
            "missing": S.page_click(
                selector={"css": "#missing-target"}, session_id=scratch_session
            ),
            "ambiguous": S.page_click(
                selector={"text": "Duplicate target", "exact": True},
                session_id=scratch_session,
            ),
            "closed": S.page_click(
                selector={"css": "button", "shadow": ["#closed-host"]},
                session_id=scratch_session,
            ),
            "cross_origin": S.page_click(
                selector={"css": "button", "frame": [{"css": "#opaque-frame"}]},
                session_id=scratch_session,
            ),
            "disabled": S.page_click(
                selector="#disabled-target", session_id=scratch_session
            ),
            "aria_disabled": S.page_click(
                selector={"css": "#aria-disabled-target"}, session_id=scratch_session
            ),
        }
        assert failures["missing"]["status"] == "not_found"
        assert failures["ambiguous"]["status"] == "ambiguous"
        assert failures["closed"]["status"] == "closed_shadow_root"
        assert failures["cross_origin"]["status"] == "cross_origin_frame"
        assert failures["disabled"]["status"] == "not_interactable"
        assert failures["aria_disabled"]["status"] == "not_interactable"

        observed = S.execute_js(
            """
            const root = document.querySelector('#open-host').shadowRoot;
            return JSON.stringify({
              events: window.__locatorEvents,
              topValue: document.querySelector('#label-target').value,
              shadowValue: root.querySelector('#shadow-input').value,
            });
            """,
            session_id=scratch_session,
            no_monitor=True,
            timeout=15,
        )
        assert json.loads(observed["js_return"]) == {
            "events": ["role", "text", "deep", "shadow"],
            "topValue": "top-level",
            "shadowValue": "inside-shadow",
        }

    def test_page_drag_events_reach_scratch_without_raising_it(self, scratch_session):
        def all_tabs():
            reply = S.list_all_tabs(session_id=scratch_session)
            return reply.get("data") or reply.get("result", {}).get("data", [])

        def is_active(tab_id):
            return next((tab.get("active") for tab in all_tabs()
                         if tab.get("id") == tab_id), None)

        def reset_events():
            S.execute_js(
                "window.__pageInputEvents = []; return true",
                session_id=scratch_session,
                no_monitor=True,
                timeout=15,
            )

        def observed_events():
            observed = S.execute_js(
                "return JSON.stringify(window.__pageInputEvents)",
                session_id=scratch_session,
                no_monitor=True,
                timeout=15,
            )
            return json.loads(observed["js_return"])

        def assert_background_result(result, foreground_tab_id):
            assert result["input_mode"] == "cdp"
            assert result["foreground_changed"] is False
            assert is_active(foreground_tab_id) is True

        original_active = next((tab for tab in all_tabs() if tab.get("active")), None)
        client_id = str(scratch_session).rsplit(":", 1)[0]
        other_sessions = [
            tab for tab in S.compact_tabs(fresh=True)
            if tab["id"] != scratch_session
            and str(tab["id"]).rsplit(":", 1)[0] == client_id
        ]
        if not other_sessions:
            pytest.skip("need another connected tab in the scratch tab's browser")
        foreground_session = other_sessions[0]["id"]
        foreground_tab_id = int(str(foreground_session).rsplit(":", 1)[-1])

        try:
            goto(scratch_session, STATIC, "h1")
            setup = S.execute_js(
                """
                document.body.innerHTML = `
                  <form id="page-input-form">
                    <input id="page-input-field" value="old">
                    <button id="page-input-button" type="button">Click</button>
                  </form>
                  <div class="xterm" id="page-input-xterm">
                    <textarea id="page-input-xterm-helper" class="xterm-helper-textarea"
                              aria-label="Terminal input"></textarea>
                  </div>
                  <div id="page-input-drag" style="width:240px;height:100px"></div>`;
                window.__pageInputEvents = [];
                for (const type of ['click', 'input', 'keydown', 'keyup',
                                    'mousedown', 'mousemove', 'mouseup']) {
                  document.addEventListener(type, event => {
                    window.__pageInputEvents.push({
                      type,
                      id: event.target && event.target.id || '',
                      key: event.key || '',
                      ctrlKey: Boolean(event.ctrlKey),
                      altKey: Boolean(event.altKey),
                      shiftKey: Boolean(event.shiftKey),
                      metaKey: Boolean(event.metaKey),
                      clientX: Number.isFinite(event.clientX) ? event.clientX : null,
                      clientY: Number.isFinite(event.clientY) ? event.clientY : null,
                      button: Number.isFinite(event.button) ? event.button : null,
                      buttons: Number.isFinite(event.buttons) ? event.buttons : null
                    });
                  }, true);
                }
                document.querySelector('#page-input-form').addEventListener('submit', event => {
                  event.preventDefault();
                  window.__pageInputEvents.push({type: 'submit', id: event.target.id, key: ''});
                });
                const rect = document.querySelector('#page-input-drag').getBoundingClientRect();
                return JSON.stringify({
                  x1: rect.left + 10, y1: rect.top + 20,
                  x2: rect.right - 10, y2: rect.top + 20
                });
                """,
                session_id=scratch_session,
                no_monitor=True,
                timeout=15,
            )
            drag = json.loads(setup["js_return"])

            S.activate_tab(session_id=foreground_session)
            assert is_active(foreground_tab_id) is True

            reset_events()
            click = S.page_click(selector="#page-input-button",
                                 session_id=scratch_session)
            assert_background_result(click, foreground_tab_id)
            click_events = observed_events()
            assert any(event["type"] == "click" and event["id"] == "page-input-button"
                       for event in click_events)

            reset_events()
            typed = S.page_type("hello", selector="#page-input-field", clear=True,
                                session_id=scratch_session)
            assert_background_result(typed, foreground_tab_id)
            type_events = observed_events()
            assert any(event["type"] == "input" and event["id"] == "page-input-field"
                       for event in type_events)
            typed_value = S.execute_js(
                "return document.querySelector('#page-input-field').value",
                session_id=scratch_session,
                no_monitor=True,
                timeout=15,
            )
            assert typed_value["js_return"] == "hello"

            reset_events()
            xterm_typed = S.page_type(
                "printf 'btap-xterm'",
                selector="#page-input-xterm",
                submit_key="enter",
                session_id=scratch_session,
            )
            assert_background_result(xterm_typed, foreground_tab_id)
            xterm_state = S.execute_js(
                "return JSON.stringify({value: document.querySelector('.xterm-helper-textarea').value, "
                "active: document.activeElement === document.querySelector('.xterm-helper-textarea')})",
                session_id=scratch_session,
                no_monitor=True,
                timeout=15,
            )
            xterm_state = json.loads(xterm_state["js_return"])
            assert xterm_state == {"value": "printf 'btap-xterm'", "active": True}
            xterm_events = observed_events()
            assert any(event["type"] == "input" and event["id"] == "page-input-xterm-helper"
                       for event in xterm_events)
            assert any(event["type"] == "keydown" and event["key"] == "Enter"
                       for event in xterm_events)

            reset_events()
            pressed = S.page_press("ctrl,a", session_id=scratch_session)
            assert_background_result(pressed, foreground_tab_id)
            press_events = observed_events()
            key_events = [event for event in press_events
                          if event["type"] in ("keydown", "keyup")]
            assert any(event["type"] == "keydown" and event["key"].lower() == "a"
                       and event["ctrlKey"] is True for event in key_events)
            assert any(event["type"] == "keyup" and event["key"].lower() == "a"
                       and event["ctrlKey"] is True for event in key_events)
            assert any(event["key"] == "Control" for event in key_events)

            reset_events()
            dragged = S.page_drag(drag["x1"], drag["y1"], drag["x2"], drag["y2"],
                                  session_id=scratch_session)
            assert_background_result(dragged, foreground_tab_id)
            drag_events = [event for event in observed_events()
                           if event["id"] == "page-input-drag"]
            assert any(event["type"] == "mousedown" for event in drag_events)
            assert any(event["type"] == "mouseup" for event in drag_events)
            move_points = {
                (event["clientX"], event["clientY"])
                for event in drag_events
                if event["type"] == "mousemove"
            }
            assert len(move_points) >= 2
        finally:
            if original_active is not None:
                S.require_driver().ext_cmd(
                    {"cmd": "tabs", "method": "switch", "tabId": original_active["id"]},
                    client_id=client_id,
                    timeout=15.0,
                )

    def test_page_type_autofocuses_single_xterm_from_body(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        tabs_before = S.list_all_tabs(session_id=scratch_session).get("data", [])
        active_before = next((tab["id"] for tab in tabs_before if tab.get("active")), None)
        setup = S.execute_js(
            """
            document.body.innerHTML = `
              <div class="xterm">
                <div class="xterm-screen">
                  <textarea class="xterm-helper-textarea"
                            aria-label="Terminal input"></textarea>
                </div>
              </div>`;
            const helper = document.querySelector('.xterm-helper-textarea');
            window.__btapXtermEvents = [];
            for (const type of ['focus', 'beforeinput', 'input']) {
              helper.addEventListener(type, event => {
                window.__btapXtermEvents.push({
                  type,
                  data: event.data || null,
                  value: helper.value,
                });
              });
            }
            document.body.tabIndex = -1;
            helper.blur();
            document.body.focus();
            return JSON.stringify({
              active: document.activeElement.tagName,
              helpers: document.querySelectorAll('.xterm-helper-textarea').length,
            });
            """,
            session_id=scratch_session,
            no_monitor=True,
            timeout=15,
        )
        assert json.loads(setup["js_return"]) == {"active": "BODY", "helpers": 1}

        result = S.page_type("BTAP_FIRST_INPUT_OK", session_id=scratch_session)
        observed = S.execute_js(
            """
            const helper = document.querySelector('.xterm-helper-textarea');
            return JSON.stringify({
              active: document.activeElement === helper,
              value: helper.value,
              events: window.__btapXtermEvents,
            });
            """,
            session_id=scratch_session,
            no_monitor=True,
            timeout=15,
        )
        tabs_after = S.list_all_tabs(session_id=scratch_session).get("data", [])
        active_after = next((tab["id"] for tab in tabs_after if tab.get("active")), None)
        state = json.loads(observed["js_return"])

        assert result["status"] == "success"
        assert result["foreground_changed"] is False
        assert result["target_kind"] == "xterm"
        assert result["typed_chars"] == len("BTAP_FIRST_INPUT_OK")
        assert active_after == active_before
        assert state["active"] is True
        assert state["value"] == "BTAP_FIRST_INPUT_OK"
        assert any(event["type"] == "focus" for event in state["events"])
        assert any(
            event["type"] == "input" and event["data"] == "BTAP_FIRST_INPUT_OK"
            for event in state["events"]
        )

    def test_page_type_structured_locator_executes_in_background(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        S.execute_js(
            "document.body.innerHTML='<label for=btap-field>BTAP field</label>' + "
            "'<input id=btap-field>'; return true",
            session_id=scratch_session,
            no_monitor=True,
        )

        result = S.page_type(
            "background-value",
            selector={"label": "BTAP field"},
            clear=True,
            session_id=scratch_session,
        )
        value = S.execute_js(
            "return document.querySelector('#btap-field').value",
            session_id=scratch_session,
            no_monitor=True,
        )

        assert result["status"] == "success"
        assert result["foreground_changed"] is False
        assert value["js_return"] == "background-value"

    def test_page_press_dispatches_chord_in_background(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        S.execute_js(
            "document.body.innerHTML='<input id=btap-key-target value=abcdef>'; "
            "document.querySelector('#btap-key-target').focus(); "
            "window.__btapKeys=[]; document.addEventListener('keydown', "
            "e => window.__btapKeys.push([e.key,e.ctrlKey]), true); return true",
            session_id=scratch_session,
            no_monitor=True,
        )

        result = S.page_press("ctrl,a", session_id=scratch_session)
        observed = S.execute_js(
            "return JSON.stringify(window.__btapKeys)",
            session_id=scratch_session,
            no_monitor=True,
        )

        assert result["status"] == "success"
        assert result["foreground_changed"] is False
        keys = json.loads(observed["js_return"])
        assert ["a", True] in keys


class TestFailoverRefusal:
    def test_dead_session_is_refused_not_redirected(self, driver):
        """A script aimed at a dead tab must not run on a different live tab:
        'click checkout' landing on the wrong page is worse than an error."""
        with pytest.raises(ValueError) as exc:
            driver.execute_js("return 1", timeout=5,
                              session_id="chrome_nonexistent:999999")
        msg = str(exc.value)
        assert "not connected" in msg
        # And it should tell the caller what it could have used instead.
        if driver.get_all_sessions():
            assert "switch_tab" in msg or "Active sessions" in msg

    def test_scan_page_does_not_silently_switch_tabs(self, driver):
        """Reading is side-effect free, but silently returning a DIFFERENT page
        than the caller asked for is its own wrong answer, so get_main_block
        defaults to no failover too."""
        from browsertap_mcp import simphtml

        prev = driver.default_session_id
        try:
            driver.default_session_id = "chrome_nonexistent:999999"
            with pytest.raises(Exception) as exc:
                simphtml.get_main_block(driver, timeout=10)
            assert "not connected" in str(exc.value)
        finally:
            driver.default_session_id = prev

    def test_get_cookies_refuses_a_dead_explicit_session(self):
        with pytest.raises(Exception):
            S.get_cookies(session_id="chrome_nonexistent:999999")

    def test_switch_tab_refuses_a_dead_explicit_session(self):
        with pytest.raises(Exception):
            S.switch_tab(session_id="chrome_nonexistent:999999")

    def test_failover_is_available_when_explicitly_asked_for(self, driver, scratch_session):
        """The escape hatch still works for a caller that genuinely wants any
        live tab rather than a specific one."""
        from browsertap_mcp import simphtml

        prev = driver.default_session_id
        try:
            driver.default_session_id = "chrome_nonexistent:999999"
            block = simphtml.get_main_block(driver, timeout=25, allow_failover=True)
            # The target is deliberately "any live tab", so its exact DOM and
            # serialized length are unconstrained.  A non-empty block proves
            # the explicitly requested failover reached a live page.
            assert isinstance(block, str)
            assert block.strip()
        finally:
            driver.default_session_id = prev


class TestActivateTab:
    def test_activate_reports_the_tab(self, scratch_session):
        r = S.activate_tab(session_id=scratch_session)
        assert r["status"] == "ok"
        assert r["activated_session_id"] == scratch_session
        assert isinstance(r["tab_id"], int)

    @staticmethod
    def _is_active(session_id):
        """Whether the tab is the active one in its window.

        Deliberately not document.visibilityState: that also reads `hidden`
        when the Chrome window is merely minimised, which a test cannot
        control. `active` is what activation actually promises.
        """
        tab_id = int(str(session_id).rsplit(":", 1)[-1])
        r = S.list_all_tabs()
        for t in r.get("data") or r.get("result", {}).get("data", []):
            if t["id"] == tab_id:
                return t["active"]
        return None

    def test_activate_tab_really_raises_the_tab(self, driver, scratch_session):
        """The report is not the proof — check Chrome agrees."""
        S.activate_tab(session_id=scratch_session)
        assert self._is_active(scratch_session) is True

    def test_switch_tab_is_background_by_default(self, driver, scratch_session):
        """Re-targeting a tab must not steal the browser foreground."""
        S.activate_tab(session_id=scratch_session)
        others = [t for t in S.compact_tabs(fresh=True) if t["id"] != scratch_session]
        if not others:
            pytest.skip("need a second tab to prove the switch moved anything")
        S.switch_tab(session_id=others[0]["id"])
        assert self._is_active(scratch_session) is True

        r = S.switch_tab(session_id=others[0]["id"], activate=True)
        assert r["active_session_id"] == others[0]["id"]
        assert "activation_failed" not in r
        assert self._is_active(others[0]["id"]) is True

    def test_switch_tab_opt_out_leaves_the_screen_alone(self, driver, scratch_session):
        """activate=false still has to re-target the bridge, just without
        stealing the user's foreground tab."""
        S.activate_tab(session_id=scratch_session)
        others = [t for t in S.compact_tabs(fresh=True) if t["id"] != scratch_session]
        if not others:
            pytest.skip("need a second tab")

        r = S.switch_tab(session_id=others[0]["id"], activate=False)
        assert r["active_session_id"] == others[0]["id"]
        assert "activated" not in r
        assert self._is_active(scratch_session) is True  # untouched

    def test_activate_tab_cleanup_restores_original_active_tab(self, scratch_session):
        tabs = S.list_all_tabs(session_id=scratch_session).get("data", [])
        original = next((tab for tab in tabs if tab.get("active")), None)
        if original is None:
            pytest.skip("no active tab to restore")
        client_id = str(scratch_session).rsplit(":", 1)[0]
        try:
            S.activate_tab(session_id=scratch_session)
            assert self._is_active(scratch_session) is True
        finally:
            S.require_driver().ext_cmd(
                {"cmd": "tabs", "method": "switch", "tabId": original["id"]},
                client_id=client_id,
                timeout=15.0,
            )
        assert self._is_active(f"{client_id}:{original['id']}") is True


class TestCookiesAndStorage:
    def test_set_cookies_success(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        name = "btap_set_cookies_success"
        try:
            result = S.set_cookies(
                {"name": name, "value": "v1", "path": "/"},
                session_id=scratch_session,
            )
            assert result["status"] == "ok", result
        finally:
            S.delete_cookies(name, session_id=scratch_session)

    def test_set_cookies_cleanup_removes_fixture_cookie(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        name = "btap_set_cookies_cleanup"
        S.set_cookies({"name": name, "value": "v1", "path": "/"}, session_id=scratch_session)
        try:
            assert any(
                cookie["name"] == name
                for cookie in S.get_cookies(session_id=scratch_session).get("data", [])
            )
        finally:
            S.delete_cookies(name, session_id=scratch_session)
        assert not any(
            cookie["name"] == name
            for cookie in S.get_cookies(session_id=scratch_session).get("data", [])
        )

    def test_get_cookies_success(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        name = "btap_get_cookies_success"
        S.set_cookies({"name": name, "value": "v1", "path": "/"}, session_id=scratch_session)
        try:
            cookies = S.get_cookies(session_id=scratch_session).get("data", [])
            assert [cookie["value"] for cookie in cookies if cookie["name"] == name] == ["v1"]
        finally:
            S.delete_cookies(name, session_id=scratch_session)

    def test_delete_cookies_success(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        name = "btap_delete_cookies_success"
        S.set_cookies({"name": name, "value": "v1", "path": "/"}, session_id=scratch_session)
        result = S.delete_cookies(name, session_id=scratch_session)
        assert result["status"] == "ok", result

    def test_delete_cookies_cleanup_leaves_no_fixture_cookie(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        name = "btap_delete_cookies_cleanup"
        S.set_cookies({"name": name, "value": "v1", "path": "/"}, session_id=scratch_session)
        try:
            S.delete_cookies(name, session_id=scratch_session)
        finally:
            S.delete_cookies(name, session_id=scratch_session)
        assert not any(
            cookie["name"] == name
            for cookie in S.get_cookies(session_id=scratch_session).get("data", [])
        )

    def test_storage_set_roundtrip(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        s = S.storage_set("lk", "lv", session_id=scratch_session)
        assert s["status"] == "success", s
        r = S.storage_get("lk", session_id=scratch_session)
        assert r["found"] and r["value"] == "lv"

    def test_storage_set_cleanup_removes_fixture_key(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        S.storage_set("btap_cleanup", "value", session_id=scratch_session)
        try:
            assert S.storage_get("btap_cleanup", session_id=scratch_session)["found"]
        finally:
            S.execute_js(
                "localStorage.removeItem('btap_cleanup'); return true",
                session_id=scratch_session,
                no_monitor=True,
            )
        assert not S.storage_get("btap_cleanup", session_id=scratch_session)["found"]

    def test_storage_get_dump_all(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        S.storage_set("lk2", "lv2", session_id=scratch_session)
        r = S.storage_get(session_id=scratch_session)
        assert r["status"] == "success"
        assert r["items"].get("lk2") == "lv2"


class TestWaitForUrl:
    def test_waits_for_navigation_to_complete(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        S.execute_js('location.href="https://example.org/"',
                     session_id=scratch_session, no_monitor=True, timeout=10)
        r = S.wait_for_url(r"example\.org", timeout=20, session_id=scratch_session)
        assert r["status"] == "success", r
        assert "example.org" in r["url"]
        assert r["ready_state"] == "complete"

    def test_substring_pattern_works(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        S.execute_js('location.href="https://example.org/"',
                     session_id=scratch_session, no_monitor=True, timeout=10)
        r = S.wait_for_url("example.org", timeout=20, session_id=scratch_session)
        assert r["status"] == "success", r

    def test_times_out_when_pattern_never_matches(self, scratch_session):
        goto(scratch_session, STATIC, "h1")
        r = S.wait_for_url(r"definitely-not-here\.xyz", timeout=3,
                           session_id=scratch_session)
        assert r["status"] == "timeout"
        # It must actually have waited, not returned instantly.
        assert r["waited_ms"] >= 2500, r["waited_ms"]
