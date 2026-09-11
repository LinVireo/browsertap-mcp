# Native file-dialog decision and contract

Read before changing `native_dialog.py` or its two public tools. Page work uses
CDP; uploads use `upload_files` on the page input. A real native file picker is
outside the page event model, so cancellation has a separate explicit boundary.

## Decision

Expose inspection and cancellation only for a foreground Windows standard Shell
file dialog owned by a registered Chrome or Edge process. Both calls require
`desktop_opt_in=true` and the desktop extra. The removed global mouse, keyboard
and desktop-screenshot tools remain removed. Portable/unregistered browsers,
cross-process brokers, unrecognized native controls and other platforms refuse.

The API accepts no arbitrary window handle, coordinates, file contents or title.
Inspection returns a ticket valid for 15 seconds in the creating MCP process;
cancellation uses that ticket. Safe mode requires physical approval; default lab
skips elicitation while retaining opt-in and every targeting/input check.

## Identity and dispatch

Identity uses PID/thread, process creation time and full image path matched to
Windows App Paths, a bounded same-process owner chain rooted in a browser window,
and standard Shell filename/type/list controls with a direct Cancel button.
Checks include foreground/focus, visibility, cloaking, monitor bounds and Cancel
hit points. Top-level z-order also checks disabled overlays, which
`WindowFromPoint` skips. DPI awareness is scoped to the calling worker thread.

Each inspection adds a random window-property name and value. HWND reuse within
one process cannot inherit this lifetime marker. Inspection is therefore a
`mixed` side effect, with `target=none`; cancellation is `write`, with the ticket
as its required target. Both belong to the desktop capability registry.

An opted-in cancel attempt claims its ticket before awaiting approval, so a
concurrent caller cannot approve or consume the same ticket. The record remains
subject to expiry and capacity limits during approval. Cancellation or failure
while awaiting approval invalidates the ticket and cleans its matching marker.
Approval, TTL, dependency or lease refusals also consume the attempt.
After acquiring `PhysicalInputLease`, cancellation uses one thread DPI context
for the quiet window and input samples before/after identity checks. It requires
an enforced `os_last_input_time` observation, checks currently held keys/buttons,
and revalidates foreground. Pointer-only or missing input telemetry is not enough.

Send one `SendMessageTimeoutW(BM_CLICK)` to the verified Cancel control with a
750 ms bound, then observe closure for at most 500 ms while holding the lease.
Do not activate a window, send global Enter/Escape, or use `WM_CLOSE`/coordinates.
The final Win32 check and message send cannot form an atomic desktop transaction.

## Results and cleanup

Only observing that the original dialog HWND no longer exists produces
`status="success", cancelled=true`. A message acknowledgement, missing marker,
or reused HWND alone does not prove closure. Unknown delivery/closure retains
`retry_safe=false`, `may_have_executed=true` and diagnostic evidence; do not replay.

Inspection reports that no quiet gate ran. Both tools include `desktop`,
`on_screen` and `input_quiet`; unobserved fields remain unknown. Diagnostic output
omits image paths, titles, selected filenames and property secrets.

Tickets are bounded to eight per process. Consumption, expiry, eviction and
orderly shutdown remove only that ticket's matching marker. Current-call cleanup
failure is reported as `marker_cleanup_verified=false`; API failure or abrupt
process death can prevent removal, so do not promise unconditional cleanup.

## Validation

`tests/test_native_dialog.py` covers opt-in, approval, single-use/expiring tickets,
same-process HWND reuse, input/focus changes, lease/refusal cleanup and uncertain
dispatch. `tests/test_native_dialog_win32.py` exercises ABI declarations and
adapter failure paths with synthetic DLLs and registry data.

Those tests do not establish real browser/native-layout compatibility. Native
acceptance must open a file dialog from a task-owned page, record why page input
cannot cancel it, exercise the explicit tools, observe actual closure, and clean
up only the task's page. Record the OS, browser, Python source identity and loaded
extension identity; unavailable platforms or stale builds remain separate limits.
