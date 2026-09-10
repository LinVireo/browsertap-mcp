# Implementation plan

1. `src/browsertap_mcp/server.py`
   - add `_resolve_cdp_target`, rewrite `_cdp` on top of `_direct_cdp`
   - add `_extension_batch`; use it in `cdp_batch` and `upload_files`
   - `cdp_batch`: validate the decoded JSON is an object
   - `page_type`: guard command gets `"cmd": "cdp"`
   - reword the two "via Hermes" messages
2. `src/browsertap_mcp/page_input.py`
   - `press_commands`: `text` on the final keyDown (Enter -> "\r", printable char)
   - `ChallengeAttemptTracker.record`: refresh `started_at` on each attempt
3. Tests
   - `tests/test_offline.py`: upload_files tests use a driver with `ext_cmd`;
     the page_type batch assertion covers `cmd` on every command
   - `tests/test_all_tools_behavior.py`: cdp_batch success path via `ext_cmd`
   - `tests/test_page_input.py`: press text contract + tracker idleness
   - `tests/test_live_browser.py`: cookies assert `method == "cdp"`; new live
     checks for `cdp_batch`, `upload_files`, Enter submit
   - new offline regression: `set_cookies` routes through `ext_cmd` with
     `cmd == "cdp"`, never as a text script
4. Validation
   - `python -m pytest tests/ -q` (offline, expect >= 2595 passed)
   - `python -m pytest tests/test_live_browser.py -q -m live -k "cookies or batch or upload or page_input"`
   - `python -m scripts.check_tool_docs`
5. CHANGELOG `[Unreleased]` entries under Fixed
6. Assessment document: `docs/`? No -- deliverable goes to the task dir
   (`assessment.md`) and the final report; nothing under `docs/` is published
   for this.

Rollback: every change is server-side Python; `git checkout -- <file>` restores.
No extension reload is required.
