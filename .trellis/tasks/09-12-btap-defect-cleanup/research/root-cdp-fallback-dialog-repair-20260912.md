# Python CDP fallback dialog repair

Owner: `codex-resume-01a0881c`, 2026-09-12. This is a follow-up to the
historical source-reconciliation records; those records remain unchanged.

The Python CDP fallback in `src/browsertap_mcp/server.py` still used the old
`window.__btap_dialog_scopes` convention after
`chrome_extension/disable_dialogs.js` moved to the `manageDialogScope`
controller. As a result, fallback `accept`/`dismiss` calls could execute the
script without intercepting native `alert`, `confirm`, or `prompt` calls.

The fallback now imports the same controller source as the extension, enters
and releases an explicit lease, and returns a `__btap_dialog_result` envelope
only when a dialog was observed. The no-dialog path keeps the prior scalar
return contract. Regression coverage exercises both policies, confirm/prompt
return values, recorded dialogs, descriptor restoration, controller cleanup,
and native-dialog restoration after execution.

Runtime identity now includes `chrome_extension/disable_dialogs.js`; the
identity test inventory consequently covers five imported JavaScript assets.
The focused dialog/serialization suites, runtime-identity suite, full offline
suite, Ruff, mypy, targeted ESLint, compileall, and `git diff --check` passed
before the final committed-tree seal. The new extension has not been manually
reloaded and this record makes no live-build claim.
