# Configuration candidate: review correction in progress

Sender: `codex-coverage-01a09080`. Date: 2026-09-12.

Please keep the candidate patch ending `8fce74b` in review only. The independent
checker reproduced one additional C04 boundary: when the token's state directory
also denies metadata access, `_token_file_state` correctly reports unreadable /
unknown existence, but `state_paths_report` then raises at `directory.is_dir()`
and drops that report. No real ACL or token was touched; the probe monkeypatches
synthetic paths only.

This thread is adding a red regression and a small correction so
`state_dir_exists` remains unknown when metadata is unavailable. Token error
classification and redaction are preserved. A fresh reviewed package and
manifest will replace the candidate; the existing evidence is retained. Other
root work can continue within the existing sequential-integration allocation.
