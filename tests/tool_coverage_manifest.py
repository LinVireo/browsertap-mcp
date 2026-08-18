"""Behavior-evidence contract for every registered MCP tool."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolCoverage:
    layer: Literal["offline", "harness", "live"]
    success_case: str
    boundary_case: str
    mutates_state: bool = False
    cleanup_case: str | None = None
    harness_reason: str | None = None


H = "tests/test_all_tools_behavior.py"
L = "tests/test_locator_contract.py"


def harness(tool: str, *, mutates: bool = False, reason: str) -> ToolCoverage:
    return ToolCoverage(
        layer="harness",
        success_case=f"{H}::test_harness_success[{tool}]",
        boundary_case=f"{H}::test_harness_boundary[{tool}]",
        mutates_state=mutates,
        cleanup_case=f"{H}::test_harness_cleanup[{tool}]" if mutates else None,
        harness_reason=reason,
    )


TOOL_COVERAGE: dict[str, ToolCoverage] = {
    "activate_tab": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestActivateTab::test_activate_tab_really_raises_the_tab",
        "tests/test_offline.py::test_activate_tab_admits_a_window_it_could_not_raise",
        True,
        "tests/test_live_browser.py::TestActivateTab::test_activate_tab_cleanup_restores_original_active_tab",
    ),
    "call_extension": ToolCoverage(
        "offline",
        "tests/test_phase1_tools.py::test_call_extension_parses_json_and_preserves_structured_failure",
        "tests/test_phase1_tools.py::test_call_extension_rejects_invalid_json_before_bridge",
    ),
    "capture_desktop_screenshot": harness(
        "capture_desktop_screenshot", reason="desktop capture had no controlled offline behavior test"
    ),
    "capture_page_screenshot": ToolCoverage(
        "offline",
        "tests/test_screenshot_content.py::test_capture_page_screenshot_attaches_image_even_when_saved",
        "tests/test_screenshot_content.py::test_capture_page_screenshot_rejects_dead_or_mismatched_target",
    ),
    "cdp_batch": harness("cdp_batch", reason="the batch wrapper lacked focused validation evidence"),
    "cdp_command": ToolCoverage(
        "offline",
        "tests/test_phase0_recovery.py::test_cdp_command_accepts_full_session_or_numeric_tab",
        "tests/test_phase0_recovery.py::test_cdp_command_external_debugger_conflict_preserves_error_and_cleans_state",
    ),
    "close_tabs": ToolCoverage(
        "offline",
        "tests/test_phase0_recovery.py::test_close_tabs_closes_live_owned_tabs_and_skips_user_closed_owned_tabs",
        "tests/test_phase0_recovery.py::test_close_tabs_default_refuses_preexisting_user_tab",
        True,
        "tests/test_phase0_recovery.py::test_close_tabs_treats_owned_session_that_is_already_gone_as_user_closed",
    ),
    "console_capture_start": ToolCoverage(
        "offline",
        f"{H}::test_harness_success[console_capture_start]",
        f"{H}::test_harness_boundary[console_capture_start]",
        True,
        f"{H}::test_harness_cleanup[console_capture_start]",
    ),
    "console_capture_stop": ToolCoverage(
        "offline",
        f"{H}::test_harness_success[console_capture_stop]",
        f"{H}::test_harness_boundary[console_capture_stop]",
        True,
        f"{H}::test_harness_cleanup[console_capture_stop]",
    ),
    "create_bookmark": ToolCoverage(
        "offline",
        f"{H}::test_harness_success[create_bookmark]",
        f"{H}::test_harness_boundary[create_bookmark]",
        True,
        f"{H}::test_harness_cleanup[create_bookmark]",
    ),
    "debugger_targets": harness(
        "debugger_targets", reason="the target-listing wrapper had registration coverage only"
    ),
    "delete_cookies": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestCookiesAndStorage::test_delete_cookies_success",
        "tests/test_offline.py::TestCookieValidation::test_delete_cookies_empty_name_rejected",
        True,
        "tests/test_live_browser.py::TestCookiesAndStorage::test_delete_cookies_cleanup_leaves_no_fixture_cookie",
    ),
    "download_file": ToolCoverage(
        "offline",
        "tests/test_downloads.py::test_download_file_explicit_overwrite_replaces_requested_destination",
        "tests/test_downloads.py::test_download_file_rejects_malformed_explicit_session",
        True,
        "tests/test_downloads.py::test_download_file_default_copy_failure_is_not_published",
    ),
    "execute_js": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestNavigationOutcome::test_execute_js_plain_return_still_works",
        "tests/test_phase0_recovery.py::test_execute_js_policy_timeout_never_dispatches_fallback_script",
        True,
        "tests/test_dialog_policy.py::test_execute_js_restores_default_even_when_policy_cleanup_fails",
    ),
    "extension_path": harness(
        "extension_path", reason="the pure diagnostic wrapper had registration coverage only"
    ),
    "get_automation_profile": ToolCoverage(
        "offline",
        "tests/test_phase0_recovery.py::test_get_automation_profile_defaults_to_lab_and_env_can_force_safe",
        f"{H}::test_harness_boundary[get_automation_profile]",
    ),
    "get_bookmarks": ToolCoverage(
        "offline",
        f"{H}::test_harness_success[get_bookmarks]",
        f"{H}::test_harness_boundary[get_bookmarks]",
    ),
    "get_console_messages": ToolCoverage(
        "offline",
        f"{H}::test_harness_success[get_console_messages]",
        f"{H}::test_harness_boundary[get_console_messages]",
    ),
    "get_cookies": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestCookiesAndStorage::test_get_cookies_success",
        "tests/test_live_browser.py::TestFailoverRefusal::test_get_cookies_refuses_a_dead_explicit_session",
    ),
    "get_setup_status": harness(
        "get_setup_status", reason="setup diagnostics need deterministic bridge-up and bridge-down evidence"
    ),
    "handle_dialog": ToolCoverage(
        "offline",
        "tests/test_dialog_policy.py::test_handle_dialog_routes_action_and_prompt_to_requested_browser",
        "tests/test_dialog_policy.py::test_handle_dialog_rejects_invalid_action_without_touching_bridge",
        True,
        "tests/test_dialog_policy.py::test_handle_dialog_extension_debugger_paths_detach_in_finally",
    ),
    "hotkey": ToolCoverage(
        "offline",
        "tests/test_physical_input.py::test_accepted_physical_tool_runs_exactly_once[asyncio-hotkey-kwargs4-hotkey]",
        "tests/test_physical_input.py::test_physical_tool_requires_approval_before_any_work[asyncio-response0-hotkey-kwargs4-hotkey]",
        True,
        "tests/test_physical_input.py::test_physical_tool_releases_lease_after_success[asyncio-hotkey-kwargs4-hotkey]",
    ),
    "list_all_tabs": harness(
        "list_all_tabs", reason="the all-tab wrapper needed a direct client-routing boundary test"
    ),
    "list_extensions": harness(
        "list_extensions", reason="extension enumeration had no direct behavior evidence"
    ),
    "list_tabs": harness(
        "list_tabs", reason="diagnostic fallback behavior needed a controlled bridge failure"
    ),
    "mouse_click": ToolCoverage(
        "offline",
        "tests/test_physical_input.py::test_accepted_physical_tool_runs_exactly_once[asyncio-mouse_click-kwargs1-click]",
        "tests/test_physical_input.py::test_mouse_click_unconfirmed_target_never_receives_physical_input[asyncio-activation0]",
        True,
        "tests/test_physical_input.py::test_physical_tool_releases_lease_after_success[asyncio-mouse_click-kwargs1-click]",
    ),
    "mouse_drag": ToolCoverage(
        "offline",
        "tests/test_physical_input.py::test_accepted_physical_tool_runs_exactly_once[asyncio-mouse_drag-kwargs2-dragTo]",
        "tests/test_physical_input.py::test_physical_tool_requires_approval_before_any_work[asyncio-response0-mouse_drag-kwargs2-dragTo]",
        True,
        "tests/test_physical_input.py::test_physical_tool_releases_lease_after_success[asyncio-mouse_drag-kwargs2-dragTo]",
    ),
    "mouse_move": ToolCoverage(
        "offline",
        "tests/test_physical_input.py::test_accepted_physical_tool_runs_exactly_once[asyncio-mouse_move-kwargs0-moveTo]",
        "tests/test_physical_input.py::test_physical_tool_requires_approval_before_any_work[asyncio-response0-mouse_move-kwargs0-moveTo]",
        True,
        "tests/test_physical_input.py::test_physical_tool_releases_lease_after_success[asyncio-mouse_move-kwargs0-moveTo]",
    ),
    "network_capture_start": ToolCoverage(
        "offline",
        f"{H}::test_harness_success[network_capture_start]",
        f"{H}::test_harness_boundary[network_capture_start]",
        True,
        f"{H}::test_harness_cleanup[network_capture_start]",
    ),
    "network_capture_stop": ToolCoverage(
        "offline",
        f"{H}::test_harness_success[network_capture_stop]",
        f"{H}::test_harness_boundary[network_capture_stop]",
        True,
        f"{H}::test_harness_cleanup[network_capture_stop]",
    ),
    "open_new_tab": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestNewTabGeneration::test_open_new_tab_returns_matching_live_generation",
        "tests/test_phase0_recovery.py::test_open_new_tab_structured_probe_unknown_is_safe_before_create",
        True,
        "tests/test_phase0_recovery.py::test_open_new_tab_registers_generation_bound_agent_ownership",
    ),
    "open_url": ToolCoverage(
        "offline",
        "tests/test_dialog_policy.py::test_open_url_uses_directed_navigate_route_and_preserves_full_session",
        "tests/test_dialog_policy.py::test_open_url_rejects_invalid_policy_without_touching_bridge",
        True,
        "tests/test_live_browser.py::TestDialogPolicy::test_open_url_beforeunload_dismiss_then_accept_and_cleanup",
    ),
    "page_click": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestBackgroundPageInput::test_page_click_structured_locators_execute_against_real_dom",
        f"{L}::test_page_click_ambiguous_locator_dispatches_nothing",
        True,
        "tests/test_offline.py::test_page_click_clears_challenge_attempts_after_it_disappears",
    ),
    "page_drag": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestBackgroundPageInput::test_page_drag_events_reach_scratch_without_raising_it",
        "tests/test_page_input.py::test_page_drag_rejects_duration_outside_bounds[-0.01]",
        True,
        "tests/test_offline.py::test_page_drag_restores_default_when_batch_fails",
    ),
    "page_press": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestBackgroundPageInput::test_page_press_dispatches_chord_in_background",
        "tests/test_page_input.py::test_page_press_rejects_malformed_chords[]",
        True,
        "tests/test_offline.py::test_page_press_restores_default_when_batch_fails",
    ),
    "page_type": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestBackgroundPageInput::test_page_type_structured_locator_executes_in_background",
        f"{L}::test_page_type_unusable_locator_dispatches_nothing",
        True,
        "tests/test_offline.py::test_page_type_slow_session_resolution_sends_no_input_after_deadline",
    ),
    "pointer_info": harness(
        "pointer_info", reason="pointer diagnostics had no controlled pyautogui behavior test"
    ),
    "remove_bookmark": ToolCoverage(
        "offline",
        f"{H}::test_harness_success[remove_bookmark]",
        f"{H}::test_harness_boundary[remove_bookmark]",
        True,
        f"{H}::test_harness_cleanup[remove_bookmark]",
    ),
    "reset_site_permissions": ToolCoverage(
        "offline",
        f"{H}::test_harness_success[reset_site_permissions]",
        f"{H}::test_harness_boundary[reset_site_permissions]",
        True,
        "tests/test_site_permissions.py::test_reset_site_permissions_cleanup_lease_is_executable",
    ),
    "resolve_leave_dialog": ToolCoverage(
        "offline",
        "tests/test_phase0_recovery.py::test_resolve_leave_dialog_no_dialog_returns_without_physical_fallback[asyncio]",
        "tests/test_phase0_recovery.py::test_resolve_leave_dialog_timeout_does_not_send_physical_fallback[asyncio]",
        True,
        "tests/test_phase0_recovery.py::test_resolve_leave_dialog_normalizes_remote_json_no_dialog_error[asyncio]",
    ),
    "save_pdf": ToolCoverage(
        "offline",
        "tests/test_phase1_tools.py::test_save_pdf_validates_then_atomically_writes",
        "tests/test_phase1_tools.py::test_save_pdf_rejects_invalid_payload_without_creating_file[not-base64]",
        True,
        "tests/test_phase1_tools.py::test_save_pdf_rejects_invalid_payload_without_creating_file[bm90IGEgcGRm]",
    ),
    "scan_page": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestScanPageLinks::test_refs_replace_long_hrefs",
        "tests/test_live_browser.py::TestFailoverRefusal::test_scan_page_does_not_silently_switch_tabs",
    ),
    "scroll_page": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestScrollPage::test_bottom_then_top",
        "tests/test_live_browser.py::TestScrollPage::test_missing_selector_does_not_raise",
        True,
        "tests/test_live_browser.py::TestScrollPage::test_scroll_page_cleanup_restores_top",
    ),
    "set_automation_profile": ToolCoverage(
        "offline",
        "tests/test_phase0_recovery.py::test_set_automation_profile_is_process_local_and_validated",
        f"{H}::test_harness_boundary[set_automation_profile]",
        True,
        f"{H}::test_harness_cleanup[set_automation_profile]",
    ),
    "set_cookies": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestCookiesAndStorage::test_set_cookies_success",
        "tests/test_offline.py::TestCookieValidation::test_set_cookies_bad_json_rejected",
        True,
        "tests/test_live_browser.py::TestCookiesAndStorage::test_set_cookies_cleanup_removes_fixture_cookie",
    ),
    "set_extension_enabled": harness(
        "set_extension_enabled", mutates=True,
        reason="extension toggling needed a fake that proves both disable and restore calls",
    ),
    "set_site_permission": ToolCoverage(
        "offline",
        "tests/test_site_permissions.py::test_set_site_permission_allow_forwards_normalized_origin[asyncio]",
        "tests/test_site_permissions.py::test_set_site_permission_decline_never_sends_allow[asyncio-decline-None]",
        True,
        "tests/test_site_permissions.py::test_set_site_permission_cleanup_lease_is_executable",
    ),
    "storage_get": ToolCoverage(
        "offline",
        "tests/test_phase0_recovery.py::test_storage_get_dump_has_item_and_byte_bounds",
        "tests/test_offline.py::TestStorageValidation::test_storage_get_bad_area_rejected",
    ),
    "storage_set": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestCookiesAndStorage::test_storage_set_roundtrip",
        "tests/test_offline.py::TestStorageValidation::test_storage_set_empty_key_rejected",
        True,
        "tests/test_live_browser.py::TestCookiesAndStorage::test_storage_set_cleanup_removes_fixture_key",
    ),
    "switch_tab": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestActivateTab::test_switch_tab_is_background_by_default",
        "tests/test_live_browser.py::TestFailoverRefusal::test_switch_tab_refuses_a_dead_explicit_session",
        True,
        "tests/test_live_browser.py::TestActivateTab::test_switch_tab_opt_out_leaves_the_screen_alone",
    ),
    "type_text": ToolCoverage(
        "offline",
        "tests/test_physical_input.py::test_accepted_physical_tool_runs_exactly_once[asyncio-type_text-kwargs3-write]",
        "tests/test_physical_input.py::test_physical_tool_requires_approval_before_any_work[asyncio-response0-type_text-kwargs3-write]",
        True,
        "tests/test_physical_input.py::test_physical_tool_releases_lease_after_success[asyncio-type_text-kwargs3-write]",
    ),
    "uninstall_extension": ToolCoverage(
        "offline",
        "tests/test_phase1_tools.py::test_uninstall_extension_forwards_confirmation_and_client",
        f"{H}::test_harness_boundary[uninstall_extension]",
        True,
        f"{H}::test_harness_cleanup[uninstall_extension]",
    ),
    "upload_files": ToolCoverage(
        "offline",
        "tests/test_offline.py::test_upload_files_parses_a_bare_results_array",
        "tests/test_offline.py::test_upload_files_raises_when_selector_matches_nothing",
        True,
        "tests/test_offline.py::test_upload_files_failure_leaves_the_caller_file_untouched",
    ),
    "wait_for": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestWaitFor::test_selector_present",
        "tests/test_live_browser.py::TestWaitFor::test_absent_selector_times_out",
    ),
    "wait_for_url": ToolCoverage(
        "live",
        "tests/test_live_browser.py::TestWaitForUrl::test_substring_pattern_works",
        "tests/test_live_browser.py::TestWaitForUrl::test_times_out_when_pattern_never_matches",
    ),
}
