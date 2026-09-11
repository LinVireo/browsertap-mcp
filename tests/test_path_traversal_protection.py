"""Path traversal protection for file-writing tools."""

from pathlib import Path, PurePosixPath

import pytest

from browsertap_mcp import server as S


class TestPathTraversalProtection:
    """Verify that _validate_safe_path prevents arbitrary writes."""

    def test_reject_absolute_paths(self, tmp_path):
        with pytest.raises(ValueError, match="must be a relative path"):
            S._validate_safe_path("/etc/passwd", allowed_base=tmp_path)
        with pytest.raises(ValueError, match="must be a relative path"):
            S._validate_safe_path("C:\\Windows\\System32\\config\\SAM", allowed_base=tmp_path)

    @pytest.mark.parametrize("save_path", [
        "/etc/passwd",
        "C:/Windows/System32/config/SAM",
        r"C:\Windows\System32\config\SAM",
        r"C:relative.txt",
        r"\Windows\file.txt",
        r"\\host\share\file.txt",
        r"\\?\C:\Windows\file.txt",
    ])
    @pytest.mark.parametrize("path_flavor", ["native", "posix"])
    def test_reject_nonrelative_paths_independent_of_host(self, monkeypatch, tmp_path, save_path, path_flavor):
        if path_flavor == "posix":
            # Exercise POSIX parsing on Windows too; native tests cover filesystem resolution.
            class PosixInput(PurePosixPath):
                def expanduser(self):
                    return self

                def resolve(self):
                    return self
            monkeypatch.setattr(S, "Path", PosixInput)
            tmp_path = PosixInput("/btap-output")
        with pytest.raises(ValueError, match="must be a relative path"):
            S._validate_safe_path(save_path, allowed_base=tmp_path)

    def test_reject_parent_directory_traversal(self, tmp_path):
        with pytest.raises(ValueError, match="path traversal detected"):
            S._validate_safe_path("../../etc/passwd", allowed_base=tmp_path)
        with pytest.raises(ValueError, match="path traversal detected"):
            S._validate_safe_path("subdir/../../outside.txt", allowed_base=tmp_path)

    def test_reject_empty_path(self, tmp_path):
        with pytest.raises(ValueError, match="must not be empty"):
            S._validate_safe_path("", allowed_base=tmp_path)
        with pytest.raises(ValueError, match="must not be empty"):
            S._validate_safe_path("   ", allowed_base=tmp_path)

    def test_accept_safe_relative_paths(self, tmp_path):
        assert S._validate_safe_path("screenshot.png", allowed_base=tmp_path) == tmp_path / "screenshot.png"
        assert S._validate_safe_path("subdir/file.pdf", allowed_base=tmp_path) == tmp_path / "subdir" / "file.pdf"

    def test_default_allowed_base(self):
        expected_base = Path.home() / "Downloads" / "browsertap"
        assert S._validate_safe_path("test.png") == expected_base / "test.png"

    def test_normalizes_path_components(self, tmp_path):
        assert S._validate_safe_path("./subdir/./file.png", allowed_base=tmp_path) == tmp_path / "subdir" / "file.png"

    def test_reject_symlink_escape(self, tmp_path):
        outside = tmp_path.parent / "outside"
        outside.mkdir(exist_ok=True)
        link = tmp_path / "escape"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("Symlink creation is unavailable")
        with pytest.raises(ValueError, match="path traversal detected"):
            S._validate_safe_path("escape/file.txt", allowed_base=tmp_path)


class TestPathValidationIntegration:
    """Verify each affected tool invokes the common path guard."""

    @pytest.mark.parametrize(
        "tool",
        [
            S.save_pdf,
            S.capture_page_screenshot,
        ],
    )
    def test_tool_uses_safe_path_validator(self, tool):
        import inspect

        assert "_validate_safe_path(save_path" in inspect.getsource(tool)
