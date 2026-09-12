"""Advertised MCP hints describe every public tool's full parameter surface."""

import pytest

from browsertap_mcp import server as S

TOOLS = frozenset("""
get_automation_profile set_automation_profile get_setup_status list_tabs
list_all_tabs close_tabs switch_tab activate_tab open_url handle_dialog
resolve_leave_dialog open_new_tab extension_path list_extensions
set_extension_enabled download_file uninstall_extension get_bookmarks
create_bookmark remove_bookmark call_extension network_capture_start
network_capture_stop console_capture_start get_console_messages
console_capture_stop scan_page wait_for wait_for_url scroll_page execute_js
get_execute_js_result cdp_command save_pdf debugger_targets cdp_batch page_click
page_type page_press page_drag upload_files get_cookies set_site_permission
reset_site_permissions set_cookies delete_cookies storage_get storage_set
capture_page_screenshot inspect_native_file_dialog cancel_native_file_dialog
""".split())


@pytest.mark.anyio
async def test_every_advertised_tool_has_explicit_boolean_effect_hints():
    from browsertap_mcp.tool_annotations import TOOL_EFFECTS

    advertised = {tool.name: tool for tool in await S.mcp.list_tools()}
    assert set(advertised) == TOOLS
    assert set(TOOL_EFFECTS) == TOOLS
    assert len(advertised) == 51
    for name, tool in advertised.items():
        assert tool.annotations is not None, name
        for field in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert type(getattr(tool.annotations, field)) is bool, (name, field)


@pytest.mark.anyio
async def test_effect_hints_agree_with_the_capability_inventory():
    inventory = S.TOOL_CAPABILITIES
    for tool in await S.mcp.list_tools():
        assert tool.annotations.readOnlyHint is (inventory[tool.name]["side_effect"] == "read"), tool.name


@pytest.mark.anyio
async def test_hints_cover_mutating_optional_parameters_and_raw_execution():
    advertised = {tool.name: tool for tool in await S.mcp.list_tools()}
    for name in (
        "execute_js", "cdp_command", "cdp_batch", "call_extension",
        "scan_page", "wait_for", "get_console_messages", "capture_page_screenshot",
        "close_tabs", "set_cookies", "delete_cookies", "set_site_permission",
        "reset_site_permissions", "set_extension_enabled", "uninstall_extension",
        "page_click", "upload_files", "cancel_native_file_dialog",
    ):
        hints = advertised[name].annotations
        assert hints is not None, name
        assert hints.readOnlyHint is False, name
        assert hints.destructiveHint is True, name
        assert hints.idempotentHint is False, name
        assert hints.openWorldHint is True, name


@pytest.mark.anyio
async def test_read_observations_and_local_idempotent_setting_are_distinct():
    advertised = {tool.name: tool for tool in await S.mcp.list_tools()}
    for name in ("list_tabs", "get_bookmarks", "get_cookies", "storage_get", "wait_for_url"):
        hints = advertised[name].annotations
        assert hints is not None, name
        assert hints.readOnlyHint is True, name
        assert hints.destructiveHint is False, name
        assert hints.idempotentHint is True, name
        assert hints.openWorldHint is True, name
    local_read = advertised["get_automation_profile"].annotations
    assert local_read is not None
    assert local_read.readOnlyHint is True
    assert local_read.openWorldHint is False
    setting = advertised["set_automation_profile"].annotations
    assert setting is not None
    assert setting.readOnlyHint is False
    assert setting.destructiveHint is True
    assert setting.idempotentHint is True
    assert setting.openWorldHint is False
    for name in ("inspect_native_file_dialog", "get_execute_js_result"):
        hints = advertised[name].annotations
        assert hints is not None, name
        assert hints.readOnlyHint is False, name
        assert hints.idempotentHint is False, name


@pytest.mark.anyio
async def test_execution_descriptions_lead_with_effects_and_keep_result_contracts():
    advertised = {tool.name: tool for tool in await S.mcp.list_tools()}
    description = advertised["execute_js"].description
    assert description is not None
    assert len(description) < 1623
    assert "side effects" in description[:220]
    assert "session_id" in description[:220]
    for term in (
        "async IIFE", "wait=false", "operation_id", "get_execute_js_result",
        "same MCP session", "partial", "unknown", "retry_safe",
        "wait_for/wait_for_url", "24 KiB", "UTF-16",
        "result_file", "result_bytes", "result_sha256", "result_format",
        "result_file_scope=js-value", "result_file_encoding=json",
        "result_json", "result_json_scope=js-value",
    ):
        assert term in description, term
    for name in ("cdp_command", "cdp_batch"):
        description = advertised[name].description
        assert description is not None
        assert "Raw CDP" in description[:80]
        assert "side effects" in description[:220]
        assert "profile" in description[:220]
