"""Screen-bounds gate: refuse a point that is on no display.

The OS does not fail an out-of-range pointer move, it **clamps** it and reports
success. Measured on a 1920x1080 desktop, `SetCursorPos(2400, 1300)` returned 1
and left the cursor at (1919, 1079) -- the Windows bottom-right hot corner, so
the observable result of a coordinate meant for a 2560x1440 panel is not a
missed click but every window minimised. Nothing in pyautogui reports it.

That makes this the one gate in `physical_input` that refuses instead of
reporting: the failure is provable from the request alone. The half that has to
keep reporting is an *unreadable* rectangle, which follows `wait_for_quiet`
instead -- see `TestUnknownBounds`.
"""

from __future__ import annotations

import types

import pytest
from types import SimpleNamespace

from browsertap_mcp import physical_input as P
from browsertap_mcp import server as S

FULL_HD = {"left": 0, "top": 0, "width": 1920, "height": 1080, "source": "test"}
# A second display to the left of the primary, which is the shape
# `pyautogui.onScreen()` gets wrong -- its own docstring says it does not work
# for secondary screens.
DUAL = {"left": -1920, "top": -120, "width": 3840, "height": 1200, "source": "test"}


class TestPointsOnScreen:
    def test_interior_points_pass_and_report_the_rectangle(self):
        report = P.check_screen_bounds([(10, 10), (960, 540)], bounds=FULL_HD)

        assert report["enforced"] is True
        assert report["bounds"] == FULL_HD
        assert report["checked"] == [[10, 10], [960, 540]]
        assert "note" not in report

    def test_the_last_real_pixel_is_inside(self):
        assert P.check_screen_bounds([(1919, 1079)], bounds=FULL_HD)["enforced"] is True

    def test_a_point_on_a_second_display_is_not_refused(self):
        # The whole reason the bound is the virtual desktop and not the primary
        # screen size: this point is legitimate and outside every single monitor
        # rectangle except the one it is on.
        report = P.check_screen_bounds([(-1500, -50)], bounds=DUAL)

        assert report["enforced"] is True
        assert report["checked"] == [[-1500, -50]]

    def test_float_coordinates_are_compared_and_reported_as_pixels(self):
        report = P.check_screen_bounds([(10.7, 20.2)], bounds=FULL_HD)

        assert report["checked"] == [[10, 20]]


class TestPointsOffScreen:
    @pytest.mark.parametrize(
        "point",
        [
            (2400, 1300),   # coordinates for a 2560x1440 panel
            (-1, 500),
            (500, -1),
            (1920, 500),    # exclusive edge: a 1920-wide desktop ends at 1919
            (500, 1080),
        ],
    )
    def test_every_direction_is_refused(self, point):
        with pytest.raises(P.CoordinatesOffScreen):
            P.check_screen_bounds([point], bounds=FULL_HD)

    def test_the_edge_is_exclusive_in_the_direction_that_matters(self):
        """Comparing against the width itself is off by one *toward* the clamp.

        `x == width` is the first coordinate the OS silently pulls back onto the
        screen, so accepting it would leave the exact failure this gate exists
        for -- and it is the bug in the reference implementation this was
        modelled on, which tests `x > self.width`.
        """
        assert P.check_screen_bounds([(1919, 1079)], bounds=FULL_HD)["enforced"] is True
        with pytest.raises(P.CoordinatesOffScreen):
            P.check_screen_bounds([(1920, 1079)], bounds=FULL_HD)
        with pytest.raises(P.CoordinatesOffScreen):
            P.check_screen_bounds([(1919, 1080)], bounds=FULL_HD)

    def test_one_bad_endpoint_refuses_the_whole_drag(self):
        with pytest.raises(P.CoordinatesOffScreen) as excinfo:
            P.check_screen_bounds([(100, 100), (5000, 100)], bounds=FULL_HD)

        assert "(5000, 100)" in str(excinfo.value)
        assert "(100, 100)" not in str(excinfo.value)

    def test_the_message_names_the_geometry_and_the_clamp(self):
        """A caller cannot fix this without knowing what the real range is."""
        with pytest.raises(P.CoordinatesOffScreen) as excinfo:
            P.check_screen_bounds([(2400, 1300)], bounds=FULL_HD)

        message = str(excinfo.value)
        assert "(2400, 1300)" in message
        assert "1920x1080 at (0, 0)" in message
        assert "nothing was dispatched" in message
        assert "clamps" in message
        assert "pointer_info" in message

    def test_every_offender_is_listed_not_just_the_first(self):
        with pytest.raises(P.CoordinatesOffScreen) as excinfo:
            P.check_screen_bounds([(5000, 1), (1, 5000)], bounds=FULL_HD)

        assert "(5000, 1)" in str(excinfo.value)
        assert "(1, 5000)" in str(excinfo.value)


class TestUnknownBounds:
    """An unreadable rectangle is reported, not refused.

    Same call as `wait_for_quiet` makes for a machine with no observable input
    signal, and for the same reason: refusing would take physical input away
    from a machine where it works fine. `enforced` is what carries the
    difference outward, because a caller cannot see it from the outside.
    """

    def test_a_point_is_accepted_with_enforced_false_and_a_note(self, monkeypatch):
        monkeypatch.setattr(P, "screen_bounds", lambda: None)

        report = P.check_screen_bounds([(99999, 99999)])

        assert report["enforced"] is False
        assert report["bounds"] is None
        assert report["checked"] == [[99999, 99999]]
        assert "without being compared against any screen" in report["note"]
        assert "clamped" in report["note"]

    @pytest.mark.parametrize(
        "rect",
        [
            {},
            {"left": 0, "top": 0},
            {"left": 0, "top": 0, "width": 0, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 0},
            {"left": 0, "top": 0, "width": "wide", "height": 1080},
        ],
    )
    def test_a_rectangle_without_extent_is_unknown_not_all_outside(self, rect):
        """The dangerous misreading: a rectangle of zero area contains nothing.

        Treating it as a real bound would refuse every coordinate on a machine
        whose geometry merely came back partial -- turning a missing capability
        into a total outage of physical input.
        """
        report = P.check_screen_bounds([(500, 500)], bounds=rect)

        assert report["bounds"] is None
        assert report["enforced"] is False

    def test_no_comparable_point_is_not_reported_as_enforced(self):
        # type_text without click coordinates, or a click at the current
        # pointer: there is nothing to check, so claiming enforcement would be
        # the same silent-success shape the quiet gate had.
        report = P.check_screen_bounds([], bounds=FULL_HD)

        assert report["enforced"] is False
        assert report["checked"] == []
        assert "note" not in report


class TestUncomparablePoints:
    @pytest.mark.parametrize(
        "point",
        [
            (None, None),
            (None, 5),
            (True, 5),
            (5, False),
            ("100", 5),
            (float("nan"), 5),
            (float("inf"), 5),
        ],
    )
    def test_a_point_that_is_not_two_numbers_is_skipped_not_refused(self, point):
        report = P.check_screen_bounds([point], bounds=FULL_HD)

        assert report["checked"] == []
        assert report["enforced"] is False

    @pytest.mark.parametrize("point", [(1,), (1, 2, 3), 5, None])
    def test_a_malformed_pair_is_skipped(self, point):
        assert P.check_screen_bounds([point], bounds=FULL_HD)["checked"] == []


class TestWin32Probe:
    """The fallback must decline rather than answer from a virtualized metric."""

    @staticmethod
    def _fake_windll(aware, metrics):
        class FakeUser32:
            @staticmethod
            def GetSystemMetrics(index):
                return metrics[index]

            @staticmethod
            def IsProcessDPIAware():
                return 1 if aware else 0

        class FakeShcore:
            @staticmethod
            def GetProcessDpiAwareness(_handle, level_pointer):
                level_pointer._obj.value = 1 if aware else 0
                return 0

        return types.SimpleNamespace(user32=FakeUser32(), shcore=FakeShcore())

    # 76/77 are SM_X/YVIRTUALSCREEN, 78/79 are SM_CX/CYVIRTUALSCREEN.
    AWARE_METRICS = {76: 0, 77: 0, 78: 1920, 79: 1080}
    # What the same machine reports before anything makes it DPI-aware:
    # 1920x1080 at 125% scaling reads as 1536x864, so accepting this would
    # refuse every legitimate x from 1537 to 1919.
    UNAWARE_METRICS = {76: 0, 77: 0, 78: 1536, 79: 864}

    def test_a_dpi_aware_process_gets_the_virtual_desktop(self, monkeypatch):
        monkeypatch.setattr(
            P.ctypes, "windll", self._fake_windll(True, self.AWARE_METRICS), raising=False
        )

        assert P._win32_virtual_screen() == {
            "left": 0, "top": 0, "width": 1920, "height": 1080,
        }

    def test_an_unaware_process_declines_instead_of_reporting_a_scaled_rectangle(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            P.ctypes, "windll", self._fake_windll(False, self.UNAWARE_METRICS), raising=False
        )

        assert P._win32_virtual_screen() is None

    def test_awareness_falls_back_to_the_pre_8_1_api(self, monkeypatch):
        class FakeUser32:
            @staticmethod
            def IsProcessDPIAware():
                return 1

        monkeypatch.setattr(
            P.ctypes, "windll", types.SimpleNamespace(user32=FakeUser32()), raising=False
        )

        assert P._process_is_dpi_aware() is True

    def test_no_awareness_api_at_all_answers_unknown(self, monkeypatch):
        monkeypatch.setattr(
            P.ctypes, "windll", types.SimpleNamespace(), raising=False
        )

        assert P._process_is_dpi_aware() is None
        assert P._win32_virtual_screen() is None

    @pytest.mark.parametrize("metrics", [{76: 0, 77: 0, 78: 0, 79: 1080},
                                         {76: 0, 77: 0, 78: 1920, 79: 0}])
    def test_a_zero_extent_metric_is_not_a_rectangle(self, monkeypatch, metrics):
        monkeypatch.setattr(
            P.ctypes, "windll", self._fake_windll(True, metrics), raising=False
        )

        assert P._win32_virtual_screen() is None

    def test_a_missing_user32_is_a_missing_capability(self, monkeypatch):
        class Boom:
            @staticmethod
            def GetSystemMetrics(_index):
                raise OSError("no user32")

            @staticmethod
            def IsProcessDPIAware():
                return 1

        monkeypatch.setattr(
            P.ctypes, "windll", types.SimpleNamespace(user32=Boom()), raising=False
        )

        assert P._win32_virtual_screen() is None


class TestMssProbe:
    @staticmethod
    def _fake_mss(monitors):
        class Capture:
            def __init__(self):
                self.monitors = monitors

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        return SimpleNamespace(mss=Capture)

    def test_index_zero_is_the_virtual_desktop(self, monkeypatch):
        monkeypatch.setitem(
            P.sys.modules,
            "mss",
            self._fake_mss([
                {"left": -1920, "top": -120, "width": 3840, "height": 1200},
                {"left": -1920, "top": -120, "width": 1920, "height": 1200},
                {"left": 0, "top": 0, "width": 1920, "height": 1080},
            ]),
        )

        assert P._mss_virtual_screen() == {
            "left": -1920, "top": -120, "width": 3840, "height": 1200,
        }

    @pytest.mark.parametrize(
        "monitors", [[], [{"left": 0, "top": 0, "width": 0, "height": 0}]]
    )
    def test_no_usable_monitor_is_no_answer(self, monkeypatch, monitors):
        monkeypatch.setitem(P.sys.modules, "mss", self._fake_mss(monitors))

        assert P._mss_virtual_screen() is None

    def test_a_missing_or_unusable_mss_is_a_missing_capability(self, monkeypatch):
        monkeypatch.setitem(P.sys.modules, "mss", None)
        assert P._mss_virtual_screen() is None

        def explode():
            raise RuntimeError("no display")

        monkeypatch.setitem(P.sys.modules, "mss", SimpleNamespace(mss=explode))
        assert P._mss_virtual_screen() is None


class TestSourceSelection:
    def test_mss_wins_because_it_needs_no_particular_load_order(self, monkeypatch):
        monkeypatch.setattr(
            P, "_mss_virtual_screen",
            lambda: {"left": 0, "top": 0, "width": 3840, "height": 1200},
        )
        monkeypatch.setattr(
            P, "_win32_virtual_screen",
            lambda: {"left": 0, "top": 0, "width": 1920, "height": 1080},
        )

        bounds = P.screen_bounds()

        assert bounds["width"] == 3840
        assert bounds["source"] == "mss_virtual_desktop"

    def test_win32_covers_an_install_without_mss(self, monkeypatch):
        monkeypatch.setattr(P, "_mss_virtual_screen", lambda: None)
        monkeypatch.setattr(
            P, "_win32_virtual_screen",
            lambda: {"left": 0, "top": 0, "width": 1920, "height": 1080},
        )

        assert P.screen_bounds()["source"] == "win32_virtual_screen"

    def test_neither_source_answering_is_unknown_not_an_error(self, monkeypatch):
        monkeypatch.setattr(P, "_mss_virtual_screen", lambda: None)
        monkeypatch.setattr(P, "_win32_virtual_screen", lambda: None)

        assert P.screen_bounds() is None

    def test_this_machine_answers_and_both_sources_agree(self):
        """Not a unit test -- the one assertion that the fakes above are honest.

        Skipped rather than failed where the geometry cannot be read, because
        that is a legitimate machine (headless CI is one) and the code's answer
        for it is already covered above.
        """
        bounds = P.screen_bounds()
        if bounds is None:
            pytest.skip("this machine exposes no readable display geometry")

        assert bounds["width"] > 0 and bounds["height"] > 0
        # Whichever source answered, the process is DPI-aware by now (mss makes
        # itself aware while constructing), so the other one must agree.
        other = P._win32_virtual_screen() if bounds["source"] == "mss_virtual_desktop" \
            else P._mss_virtual_screen()
        if other is not None:
            assert (other["width"], other["height"]) == (bounds["width"], bounds["height"])


class _FakeGui:
    def __init__(self):
        self.calls = []

    def moveTo(self, x, y, duration=0.0):
        self.calls.append(("moveTo", x, y))

    def click(self, *args, **kwargs):
        self.calls.append(("click", args, kwargs))

    def dragTo(self, x, y, duration=0.3, button="left"):
        self.calls.append(("dragTo", x, y))

    def write(self, text, interval=0.01):
        self.calls.append(("write", text))


class _Ctx:
    async def elicit(self, message, schema):
        return SimpleNamespace(action="accept", data=schema(approve=True))


def _install_tool_harness(monkeypatch, bounds=FULL_HD):
    """Run the real tool body with no lease, no quiet wait and no real input."""
    gui = _FakeGui()
    probes = []
    monkeypatch.setattr(S, "_pyautogui", lambda: gui)
    monkeypatch.setattr(S, "_maybe_activate", lambda mode, sid=None: {"on_screen": True})
    monkeypatch.setattr(
        S.physical_input, "run_physical_action", lambda summary, action: action()
    )
    # Counted, not just faked: a tool with nothing to validate must not bind the
    # display at all, and an empty `probes` is the only way to see that.
    monkeypatch.setattr(
        S.physical_input, "screen_bounds", lambda: probes.append("probe") or bounds
    )
    return gui, probes


@pytest.mark.anyio
class TestToolsRefuseBeforeDispatch:
    async def test_an_off_screen_click_dispatches_nothing(self, monkeypatch):
        gui, _probes = _install_tool_harness(monkeypatch)

        result = await S.mouse_click(ctx=_Ctx(), x=2400, y=1300, session_id="client:7")

        assert result["status"] == "coordinates_off_screen"
        assert "1920x1080" in result["message"]
        # The property that matters: not "it reported an error" but "the mouse
        # never moved". A clamped click would have looked identical in the log.
        assert gui.calls == []

    async def test_an_on_screen_click_lands_and_carries_the_report(self, monkeypatch):
        gui, _probes = _install_tool_harness(monkeypatch)

        result = await S.mouse_click(ctx=_Ctx(), x=100, y=200, session_id="client:7")

        assert result["status"] == "ok"
        assert gui.calls and gui.calls[0][0] == "click"
        assert result["screen_bounds"]["enforced"] is True
        assert result["screen_bounds"]["checked"] == [[100, 200]]

    @pytest.mark.parametrize(
        ("tool", "kwargs"),
        [
            ("mouse_move", {"x": 4000, "y": 10}),
            ("mouse_drag", {"x1": 10, "y1": 10, "x2": 4000, "y2": 10}),
            ("type_text", {"text": "secret", "click_x": 4000, "click_y": 10}),
        ],
    )
    async def test_every_coordinate_taking_tool_is_gated(self, monkeypatch, tool, kwargs):
        gui, _probes = _install_tool_harness(monkeypatch)

        result = await getattr(S, tool)(ctx=_Ctx(), session_id="client:7", **kwargs)

        assert result["status"] == "coordinates_off_screen"
        assert gui.calls == []

    async def test_a_drag_is_refused_for_its_destination_too(self, monkeypatch):
        gui, _probes = _install_tool_harness(monkeypatch)

        result = await S.mouse_drag(
            ctx=_Ctx(), x1=10, y1=10, x2=10, y2=5000, session_id="client:7"
        )

        assert result["status"] == "coordinates_off_screen"
        assert gui.calls == []

    async def test_a_click_with_no_coordinates_does_not_probe_the_display(
        self, monkeypatch
    ):
        # Wherever the pointer already is, the OS put it there, so there is
        # nothing to validate and no reason to bind the display for it.
        gui, probes = _install_tool_harness(monkeypatch)

        result = await S.mouse_click(ctx=_Ctx(), session_id="client:7")

        assert result["status"] == "ok"
        assert probes == []
        assert "screen_bounds" not in result

    async def test_a_hotkey_is_not_gated(self, monkeypatch):
        gui, probes = _install_tool_harness(monkeypatch)
        gui.hotkey = lambda *keys: gui.calls.append(("hotkey", keys))

        result = await S.hotkey(ctx=_Ctx(), keys_csv="ctrl,l", session_id="client:7")

        assert result["status"] == "ok"
        assert probes == []

    async def test_unknown_geometry_lets_the_action_through_and_says_so(
        self, monkeypatch
    ):
        gui, _probes = _install_tool_harness(monkeypatch, bounds=None)

        result = await S.mouse_click(ctx=_Ctx(), x=99999, y=99999, session_id="client:7")

        assert result["status"] == "ok"
        assert gui.calls and gui.calls[0][0] == "click"
        assert result["screen_bounds"]["enforced"] is False
        assert "without being compared" in result["screen_bounds"]["note"]

    async def test_an_unconfirmed_target_still_reports_what_was_checked(
        self, monkeypatch
    ):
        gui, _probes = _install_tool_harness(monkeypatch)
        monkeypatch.setattr(S, "_maybe_activate", lambda mode, sid=None: {"on_screen": False})

        result = await S.mouse_click(ctx=_Ctx(), x=100, y=200, session_id="client:7")

        assert result["status"] == "activation_failed"
        assert gui.calls == []
        assert result["screen_bounds"]["enforced"] is True

    async def test_the_refusal_precedes_activation(self, monkeypatch):
        """A request that cannot work must not foreground someone's tab first."""
        gui, _probes = _install_tool_harness(monkeypatch)
        activations = []
        monkeypatch.setattr(
            S, "_maybe_activate",
            lambda mode, sid=None: activations.append(sid) or {"on_screen": True},
        )

        result = await S.mouse_click(ctx=_Ctx(), x=2400, y=1300, session_id="client:7")

        assert result["status"] == "coordinates_off_screen"
        assert activations == []
