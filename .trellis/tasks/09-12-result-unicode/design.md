# Unicode result boundary

The JavaScript/JSON value model admits unpaired UTF-16 surrogates, while Python
strict UTF-8 encoding and the installed Pydantic JSON serializer reject them.
Normal `ensure_ascii=False` JSON followed by `.encode('utf-8')` fails even for
small JS values because byte sizing precedes the inline-limit decision.

Fix the boundary where the original value is still available. Preserve valid
Unicode as usual and retain any exceptional string through JSON escapes or the
existing lossless result-file contract. Test the actual outbound MCP model,
not only a stdlib JSON round trip. Escaping only TextContent is insufficient if
the same invalid scalar remains in `structuredContent`.

Keep normal results, envelope/error classification and retry decisions intact.
Any required caller-visible exceptional representation must be documented by
the coordinator. Do not broaden into SDK monkeypatching or artifact retention.

The parent also reproduced ordinary large-result file-write failure through the
real get_execute_js_result handler. An OSError after successful execution was
reclassified as retryable transport_error, losing the operation id and value.
The same bounded correction must preserve those completed receipts with an
explicit inline JSON representation when private file output is unavailable.
Its scope must distinguish a JS value from an entire result envelope or native
MCP result; nested late results must remain recoverable without replay.

The parent also reproduced a real Windows result-file path containing an
unpaired surrogate. File creation succeeds, but returning that raw path causes
the final JSONRPC serialization to fail. The final boundary must cover values
introduced during externalization itself. Encode an unsafe path as a JSON
string literal with an explicit result_file_encoding marker, and let the caller
decode that path before opening it. Clear stale markers when a descriptor is
replaced. Ordinary paths retain their existing representation. The regression
must serialize the complete outbound wire model, decode the returned path, and
read the actual file rather than stopping at model_dump(mode="json").

Real high/low-surrogate directory parameters apply to Windows NTFS. Collect
those parameters only on Windows; all hosts retain the normal-path controls and
portable surrogate-path probes. The canonical CI evidence runner rejects test
skips, so collecting an inapplicable case and skipping it inside the fixture
would break required Ubuntu CI. This changes test applicability, not the product
or the no-skip acceptance contract; foreign-platform collection simulation does
not establish a native platform run.
