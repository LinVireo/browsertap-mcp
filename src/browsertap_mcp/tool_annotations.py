"""MCP effect hints for the complete public parameter surface of each tool.

These are advisory metadata, not authorization, approval or locking rules.
Classifications cover intentional user-visible effects, including optional
code execution and output files; routine transport/bootstrap bookkeeping is
not a separate tool effect. Unknown dynamically registered tools receive the
MCP conservative defaults explicitly; this does not constrain execution.
"""

from mcp.types import ToolAnnotations

# Columns: read-only, potentially destructive, idempotent, open-world.
# A read tool's returned value may change without violating idempotence.
# Conservative non-idempotence also covers events, time-bounded leases,
# one-use tickets, per-call output files and uncertain delivery.
TOOL_EFFECTS: dict[str, tuple[bool, bool, bool, bool]] = {
    "get_automation_profile": (True, False, True, False),
    "set_automation_profile": (False, True, True, False),
    "get_setup_status": (True, False, True, True),
    "list_tabs": (True, False, True, True),
    "list_all_tabs": (True, False, True, True),
    "close_tabs": (False, True, False, True),
    "switch_tab": (False, True, False, True),
    "activate_tab": (False, True, False, True),
    "open_url": (False, True, False, True),
    "handle_dialog": (False, True, False, True),
    "resolve_leave_dialog": (False, True, False, True),
    "open_new_tab": (False, True, False, True),
    "extension_path": (True, False, True, False),
    "list_extensions": (True, False, True, True),
    "set_extension_enabled": (False, True, False, True),
    "download_file": (False, True, False, True),
    "uninstall_extension": (False, True, False, True),
    "get_bookmarks": (True, False, True, True),
    "create_bookmark": (False, False, False, True),
    "remove_bookmark": (False, True, False, True),
    "call_extension": (False, True, False, True),
    "network_capture_start": (False, True, False, True),
    "network_capture_stop": (False, True, False, True),
    "console_capture_start": (False, True, False, True),
    # clear=true consumes the entire capture buffer.
    "get_console_messages": (False, True, False, True),
    "console_capture_stop": (False, True, False, True),
    # extra_js / js accept caller code even though the common path only reads.
    "scan_page": (False, True, False, True),
    "wait_for": (False, True, False, True),
    "wait_for_url": (True, False, True, True),
    "scroll_page": (False, True, False, True),
    "execute_js": (False, True, False, True),
    # Never replays the operation, but each read can export a new result file.
    "get_execute_js_result": (False, False, False, True),
    "cdp_command": (False, True, False, True),
    "save_pdf": (False, True, False, True),
    "debugger_targets": (True, False, True, True),
    "cdp_batch": (False, True, False, True),
    "page_click": (False, True, False, True),
    "page_type": (False, True, False, True),
    "page_press": (False, True, False, True),
    "page_drag": (False, True, False, True),
    "upload_files": (False, True, False, True),
    "get_cookies": (True, False, True, True),
    "set_site_permission": (False, True, False, True),
    "reset_site_permissions": (False, True, False, True),
    "set_cookies": (False, True, False, True),
    "delete_cookies": (False, True, False, True),
    "storage_get": (True, False, True, True),
    "storage_set": (False, True, False, True),
    # save_path can replace a local file.
    "capture_page_screenshot": (False, True, False, True),
    # Installs a temporary window marker and issues a unique one-use ticket.
    "inspect_native_file_dialog": (False, False, False, True),
    "cancel_native_file_dialog": (False, True, False, True),
}


def annotations_for_tool(name: str) -> ToolAnnotations:
    read_only, destructive, idempotent, open_world = TOOL_EFFECTS.get(
        name, (False, True, False, True),
    )
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=open_world,
    )
