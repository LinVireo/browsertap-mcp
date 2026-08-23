"""Image dimensions and the pixel space they are in.

`capture_page_screenshot` returned `format`, `full_page`, `size` and nothing
else, and `size` is a byte count -- 24280 for one measured viewport shot, which
reads like a dimension if nothing says otherwise. What was missing is the
harder half: CDP hands back DEVICE pixels, CSS x devicePixelRatio, while
`page_click` takes CSS pixels. Measured on a 1920x1080 panel at 125% scaling,
the viewport is 1482x780 CSS and the screenshot is 1852x975; a real link needed
`page_click(340, 209)` while sitting at screenshot pixel (425, 261), and feeding
the screenshot pixel back made `document.elementFromPoint` answer `HTML` -- the
page background. Coordinate mode is the one path with no hit test, so nothing
reported the miss.

The dimensions are parsed out of the bytes already in hand rather than asked for
over CDP: a second batch means a second debugger attach/detach on the same tab,
measured in AGENTS.md section 4 at 15s instead of 0.16s.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from types import SimpleNamespace

import pytest

from browsertap_mcp import server as S


def _encode(width, height, fmt, **kwargs):
    """Real encoder output, so the parser is checked against real headers."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow ships in the desktop extra")
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (12, 34, 56)).save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


def _png_header(width, height):
    """A hand-built PNG header, valid as far as anything here reads it.

    Used for the tool-level tests so this module does not skip wholesale on an
    install without Pillow: only the parser tests need a real encoder, and the
    tool never decodes the pixels it forwards.
    """
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


class TestParsesRealHeaders:
    @pytest.mark.parametrize(
        ("fmt", "kwargs"),
        [
            ("PNG", {}),
            ("JPEG", {"quality": 80}),
            ("JPEG", {"progressive": True}),
            ("WEBP", {}),
            ("WEBP", {"lossless": True}),
        ],
    )
    def test_every_format_this_server_will_ask_for(self, fmt, kwargs):
        # png/jpeg/webp is exactly the set capture_page_screenshot accepts, and
        # webp has three different chunk layouts depending on the encoder.
        assert S._image_dimensions(_encode(1852, 975, fmt, **kwargs)) == (1852, 975)

    @pytest.mark.parametrize(
        ("width", "height"),
        [(1, 1), (1366, 768), (1920, 1080), (2560, 1440), (3840, 2160), (1852, 20000)],
    )
    def test_dimensions_are_not_assumed_or_clamped(self, width, height):
        # A full-page shot is routinely far taller than any panel, so nothing
        # here may bound the answer to a plausible screen size.
        assert S._image_dimensions(_encode(width, height, "PNG")) == (width, height)

    def test_width_and_height_are_not_transposed(self):
        assert S._image_dimensions(_encode(800, 200, "PNG")) == (800, 200)
        assert S._image_dimensions(_encode(200, 800, "PNG")) == (200, 800)
        assert S._image_dimensions(_encode(800, 200, "JPEG")) == (800, 200)
        assert S._image_dimensions(_encode(200, 800, "JPEG")) == (200, 800)


class TestUnparseableIsNoneNotZero:
    """A header it cannot read must not become a 0x0 image.

    Reporting zeroes would be worse than reporting nothing: a caller dividing by
    them to rescale a point gets an exception or a silent 0, where `None` plus
    the note tells it to stop.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"not an image at all",
            b"\x89PNG\r\n\x1a\nsmall-test-image",          # signature, no IHDR
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00",  # truncated IHDR
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 8,  # 0x0
            b"\xff\xd8\xff",                                # JPEG that stops
            b"\xff\xd8\xff\xc0\x00\x02",                    # SOF with no payload
            b"RIFF\x00\x00\x00\x00WEBPVP8X" + b"\x00" * 4,  # truncated VP8X
            b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 20,  # bad start code
            b"RIFF\x00\x00\x00\x00WEBPVP8L" + b"\x00" * 20,  # bad VP8L signature
            b"RIFF\x00\x00\x00\x00WEBPXXXX" + b"\x00" * 20,  # unknown chunk
        ],
    )
    def test_it_answers_none(self, raw):
        assert S._image_dimensions(raw) is None

    @pytest.mark.parametrize("raw", [None, "a string", 42, bytearray(b"\x89PNG")])
    def test_a_non_bytes_argument_does_not_raise(self, raw):
        assert S._image_dimensions(raw) is None

    def test_a_jpeg_whose_segment_chain_is_a_lie_terminates(self):
        # A zero segment length would step the cursor nowhere and loop forever.
        assert S._image_dimensions(b"\xff\xd8\xff\xe0\x00\x00" + b"\x00" * 40) is None


PNG_1852x975 = _png_header(1852, 975)
PNG_BASE64 = base64.b64encode(PNG_1852x975).decode("ascii")


class _Driver:
    def __init__(self):
        self.default_session_id = "chrome:profile:42"

    def ext_cmd(self, payload, client_id=None, timeout=15.0):
        return {"data": {"data": PNG_BASE64}}


def _install(monkeypatch, driver=None):
    driver = driver or _Driver()
    monkeypatch.setattr(S, "require_driver", lambda: driver)
    monkeypatch.setattr(S, "get_driver", lambda: driver)
    monkeypatch.setattr(
        S, "active_sessions",
        lambda timeout=None, fresh=False: [
            {"id": "chrome:profile:42", "url": "https://example.test/"}
        ],
    )
    return driver


class TestPageScreenshotReportsItsPixelSpace:
    def test_dimensions_are_reported_beside_the_byte_count(self, monkeypatch):
        _install(monkeypatch)

        out = S.capture_page_screenshot().structuredContent

        assert out["image_width"] == 1852
        assert out["image_height"] == 975
        assert out["size"] == len(PNG_1852x975)
        # The two must stay distinguishable: `size` alone was being read as a
        # dimension because it was the only number in the result.
        assert out["size"] != out["image_width"]
        assert "dimensions_note" not in out

    def test_the_space_is_named_and_the_conversion_is_stated(self, monkeypatch):
        _install(monkeypatch)

        out = S.capture_page_screenshot().structuredContent

        assert out["pixel_space"] == "device"
        note = out["model_note"]
        assert "devicePixelRatio" in note
        assert "page_click" in note
        # The safe path out of the whole problem, which the hit test covers.
        assert "scan_page" in note

    def test_the_client_side_downscale_is_named_too(self, monkeypatch):
        """A model reading a point off the picture has a second scale factor.

        Clients may shrink an attached image before the model sees it, so the
        picture's own pixels are not necessarily image_width x image_height.
        Nothing server-side can observe that, so it is stated rather than
        measured.
        """
        _install(monkeypatch)

        assert "downscale" in S.capture_page_screenshot().structuredContent["model_note"]

    def test_the_dimensions_appear_in_the_text_block_too(self, monkeypatch):
        # structuredContent is for machine consumers; the text block is what a
        # model without structured-output support actually reads.
        _install(monkeypatch)

        text = json.loads(S.capture_page_screenshot().content[0].text)

        assert text["image_width"] == 1852
        assert text["image_height"] == 975

    def test_an_unreadable_header_says_so_instead_of_dropping_the_shot(self, monkeypatch):
        class Odd(_Driver):
            def ext_cmd(self, payload, client_id=None, timeout=15.0):
                return {"data": {"data": base64.b64encode(b"\x89PNG-ish").decode("ascii")}}

        _install(monkeypatch, Odd())

        out = S.capture_page_screenshot().structuredContent

        assert out["status"] == "success"
        assert out["image_attached"] is True
        assert out["image_width"] is None
        assert out["image_height"] is None
        assert "byte count" in out["dimensions_note"]


class TestDesktopScreenshotStatesItsOwnSpace:
    def test_it_is_physical_and_unscaled(self, monkeypatch):
        class Shot:
            width = 1920
            height = 1080
            size = (1920, 1080)
            rgb = b"\x00" * (1920 * 1080 * 3)

        class Capture:
            monitors = [
                {"left": 0, "top": 0, "width": 1920, "height": 1080},
                {"left": 0, "top": 0, "width": 1920, "height": 1080},
            ]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def grab(self, monitor):
                return Shot()

        monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=Capture))

        out = S.capture_desktop_screenshot().structuredContent

        assert out["pixel_space"] == "physical"
        assert (out["width"], out["height"]) == (1920, 1080)
        # This is the pair that makes the desktop chain self-consistent: the
        # image is not resized, so its pixels *are* mouse_click's coordinates.
        assert "not resized" in out["model_note"]
        assert "mouse_click" in out["model_note"]


def test_the_two_screenshot_tools_do_not_claim_the_same_space():
    """The whole defect in one assertion: they are different spaces.

    A caller that reads one tool's description and applies it to the other gets
    coordinates off by devicePixelRatio, which on this machine is 25%.
    """
    page = S.mcp._tool_manager.get_tool("capture_page_screenshot").description
    desktop = S.mcp._tool_manager.get_tool("capture_desktop_screenshot").description

    assert "DEVICE pixels" in page
    assert "PHYSICAL screen pixels" in desktop
    assert "devicePixelRatio" in page
