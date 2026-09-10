# `open_new_tab` recovery can dead-end the caller (2026-09-10)

Found while using the tool for unrelated work, not by reading the code. Written
in a new file so the implementing session's own review record is not touched.

## What happened

Three calls, in order, all against a healthy bridge (restarted 12:29:54, doctor
`matches_tree`):

1. `open_new_tab(url="https://linux.do/")` → `status: unknown`,
   `may_have_created: false`, `retry_safe: true`, error text
   `extension chrome_5u5shi did not respond within 2.92s; the command may not
   have reached the browser (service worker asleep, ...)`.
   Per the contract this is replayable with the same `operation_id`.
2. Same call with that `operation_id` → `status: unknown`,
   `reconciliation.status: not_found`, **`may_have_created: true`**,
   `retry_safe: false`, error `operation_id was not found; it may have expired
   or belong to another client; do not replay tab creation`, plus a
   `recovery` block instructing: *"Call open_new_tab again with operation_id,
   client_id and owner_id; the recovery call only reads the durable operation
   record."*
3. Exactly that recovery call → **byte-identical response**, `not_found` again.

So: replay is forbidden (`retry_safe: false`, `may_have_created: true`), and the
recovery path the error itself prescribes returns the same `not_found` forever.
There is no field in any of the three responses that tells the caller what to do
next.

`list_tabs` before and after showed **6 tabs both times**: nothing was ever
created, so `may_have_created: true` was wrong on the facts.

A later plain `open_new_tab(url="about:blank")` succeeded first try
(`tab_id` 1935864408, closed afterwards with its `owner_id`). **This is not a
tool that always fails** -- it needs the service-worker-asleep window to enter
the recovery path at all.

## Mechanism

`server.py:3160-3170`, inside `unknown_result`:

```python
    # Reconciliation cannot prove that an earlier call never created a tab.
    # Keep its cleanup capability even when the first status read fails.
    if resuming:
        may_have_created = True
        retry_safe = False
```

That rule is right in isolation, and `server.py:3285-3289` states the read-only
intent deliberately. The dead end is the *combination* with the `not_found`
branch at `server.py:3292-3303`, which passes `may_have_created=True` for a
record that does not exist:

- The caller's own first response already established the stronger fact
  `may_have_created: false` -- the documented pre-dispatch state.
- Recovery discards that and upgrades to `true`, because `resuming` is set.
- Two calls therefore disagree about the same operation, and the contract makes
  the *less* certain one binding.

## Why this matters more than the wasted call

The tool description promises "an `operation_id`-backed exactly-once create".
Exactly-once needs an escape hatch for *did it happen or not*; here both exits
are closed at the same time. An agent following the contract literally has no
legal next action. The way out I actually used -- stop asking the tool, call
`list_tabs` and count -- is nowhere in the contract, so it works only for a
caller who already knows the tool well enough not to need the recovery block.

## First suggested direction -- SUPERSEDED, see the patch below

Recorded as written, because the reason it is wrong is the useful part:

- ~~Carry the first response's `may_have_created: false` into the recovery
  record, or key `resuming` escalation on *dispatch having been observed*.~~
- ~~When the probe is `not_found` and no dispatch was ever recorded, the honest
  answer is `may_have_created: false` / `retry_safe: true`.~~
- Whatever the verdict, the terminal `not_found` response should name the
  observation that resolves it (`list_tabs` and compare, `owner_id` to close a
  stray tab) instead of pointing back at the call that just failed.

The first two bullets assumed `not_found` proves no tab exists. It does not, and
the third bullet survives on its own. Measurements below.

Same family as the batch this task already closed: an uncertain state whose
receipt is preserved but whose exit is missing. Compare the `outcome_unknown`
work in `../../09-10-zombie-js-after-cdp-timeout/research/`.

## Re-measured before designing the patch

Five facts, all read out of the tree rather than assumed.

1. **The extension already answers honestly.** `background.js:218-221`:
   a missing record returns `may_have_created: false, retry_safe: true`.
   Python overwrites both at `server.py:3169-3171`. So no new browser-side
   plumbing is needed to carry the fact -- it already arrives and is discarded.
2. **The durable write precedes `chrome.tabs.create` and is awaited**
   (`background.js:3033-3047`, with the reason in the comment). A record is
   therefore written before any tab can exist.
3. **`pending` records are never pruned.** Both loops in
   `pruneCreateOperations` (`background.js:114`, `:120`) skip
   `status !== 'pending'`, so neither the 24h TTL nor the 256-record cap can
   drop one.
4. **But `not_found` still does not prove "no tab exists."** The store is
   `chrome.storage.session`, cleared on browser restart -- and session restore
   can reopen the tab afterwards. A `completed` record *is* subject to the TTL
   and the cap. A different browser client answers with its own store.
   So `not_found` proves *this worker never recorded the operation*, which is
   weaker than what bullets 1-2 of the superseded direction assumed.
   **The sticky escalation is therefore correct and stays.**
5. **`resume_required` has zero readers.** Three write sites
   (`server.py:3300`, `:3307`, `:3389`), no test, no doc, no code reads it
   (same shape as `[[gate-with-no-reader]]`). What an agent caller actually
   acts on is `recovery.instruction`, so that string is where the fix lands.

Coverage measurement, and the reason this shipped: **exactly one test in the
suite passes `operation_id=` to `open_new_tab`**
(`tests/test_tab_create_recovery_contract.py:121`), and its first probe is
`pending`, not `not_found`. The branch at `server.py:3292` -- resuming, first
probe `not_found` -- has **no test at all**. `server.py:3382` looks like the
same branch but is only reachable after a `pending` probe, i.e. dispatch *was*
observed; it is correct as written.

## Patch 1 -- give the terminal branch an exit (`server.py:3292-3304`)

Keeps `may_have_created: true` (fact 4) and keeps recovery read-only. Changes
what the caller is told to do, and separates two things the contract currently
conflates: *replaying this operation* (never safe) from *starting a new one*
(safe when the store was readable and held no record).

```python
        elif probe_state == "not_found":
            # The store answered and holds no record for this id. That does not
            # prove no tab exists -- storage.session is cleared on browser
            # restart and completed records age out -- so the cleanup capability
            # stays. What it does prove is that asking this tool again can only
            # ever return this same answer, so the exit has to be an observation
            # the caller makes instead of another call here.
            return unknown_result(
                {
                    **probe_info,
                    "error": (
                        "operation_id was not found in the browser's operation store; "
                        "do not replay tab creation for this operation_id"
                    ),
                    "resume_required": False,
                    "new_operation_safe": True,
                },
                may_have_created=True,
                retry_safe=False,
                terminal=True,
            )
```

`unknown_result` grows one keyword-only argument and one instruction variant.
Nothing else about it changes:

```python
    def unknown_result(
        info: Optional[dict[str, Any]] = None,
        *,
        may_have_created: bool,
        retry_safe: bool,
        terminal: bool = False,
    ) -> dict[str, Any]:
```

and inside the `if may_have_created:` block at `:3194`, the instruction becomes
a two-way choice rather than one unconditional string:

```python
            out["recovery"] = {
                "operation_id": operation_id,
                "client_id": client_id,
                "owner_id": capability_owner_id,
                "url": url,
                "instruction": (
                    # Reached only when this call already proved that another
                    # read returns the same answer. Pointing back at open_new_tab
                    # here is a loop, which is the defect this branch fixes.
                    "This operation cannot be resolved by calling open_new_tab again. "
                    "Call list_tabs and compare against url: a matching tab is this "
                    "operation's, and close_tabs accepts owner_id for it. If no tab "
                    "matches, no tab was created; start a new create with no "
                    "operation_id."
                    if terminal else
                    "Call open_new_tab again with operation_id, client_id and owner_id; "
                    "the recovery call only reads the durable operation record."
                ),
            }
```

### Deliberately not changed

- `server.py:3169-3171` -- the sticky escalation itself. Fact 4 says it is right.
- `server.py:3382` -- `not_found` *after* a `pending` probe. Dispatch was
  observed there, so "disappeared during recovery" is accurate and another read
  is not provably useless. Left non-terminal.
- `server.py:3305-3310` -- probe state `unknown`, i.e. the store could not be
  read. `loadCreateOperations` un-memoises a failed read on purpose
  (`background.js:180-183`), so a later call genuinely can succeed. Keeping
  `resume_required: True` here while the `not_found` branch drops it is the
  intended asymmetry: one is a readable store with no record, the other is no
  answer at all.
- The five other escalation call sites (`:3261`, `:3267`, `:3274`, `:3401`, and
  the retry path below `:3411`). Five of six were already correct; only the one
  provably-looping branch is in scope.

## Patch 2 -- the missing test

Fills the zero-coverage branch, and pins the loop as the thing being prevented.
Both assertions on the instruction string matter: the positive one alone would
survive a revert to the old text plus an appended sentence.

```python
def test_recovery_of_an_unknown_operation_is_not_a_loop(create_driver):
    _, statuses, _, dispatched, _ = create_driver
    statuses.append(reply("not_found"))
    result = S.open_new_tab("https://created.test/", operation_id="open-tab-fixture",
                            owner_id="owner")
    assert result["status"] == "unknown"
    # The cleanup capability survives: storage.session may have been cleared
    # after a real create.
    assert result["may_have_created"] is True
    assert result["owner_id"] == "owner"
    # Replaying this operation stays forbidden; a fresh operation is not.
    assert result["retry_safe"] is False
    assert result["reconciliation"]["new_operation_safe"] is True
    assert result["reconciliation"]["resume_required"] is False
    # The exit is an observation, not another call into this tool.
    assert "list_tabs" in result["recovery"]["instruction"]
    assert "open_new_tab again with operation_id" not in result["recovery"]["instruction"]
    assert all(call[0] == "status" for call in dispatched)
```

Mutation checks to run on a copy, each of which must fail this test:

1. Drop `terminal=True` from the `not_found` branch -> the two instruction
   assertions fail.
2. Drop `"new_operation_safe": True` -> that assertion fails.
3. Revert the escalation to `may_have_created=False` -> the `owner_id` and
   `may_have_created` assertions fail, proving the test also guards fact 4 and
   not only the new behaviour.

And one check in the other direction: `test_pending_recovery_never_recreates_a_
disappeared_operation` (`:117-125`, parametrized `not_found`/`unknown`) must
still pass untouched, since its first probe is `pending` and it therefore never
enters the terminal branch. If it goes red, the patch leaked into `:3382`.

## Patch 3 -- the caller trap, which is half mine

My first call died at `server.py:3261` (`phase: client_discovery`) -- the probe
never reached the browser, so create was **never dispatched**. That response's
own `retry_safe: true` said "retry plainly." I resumed instead. So one half of
this incident is a caller defect, and it is worth being exact about which half,
because the fix differs:

- **Both READMEs are already right.** `README.md:590` says "To recover a
  *dispatched* create", `README.zh-CN.md:537` says "恢复已投递创建时". Under that
  wording my resume was not indicated.
- **The tool description is not** (`server.py:3085`): *"If the create ACK is
  lost, call this tool again with the returned operation_id, client_id and
  owner_id"* -- unconditional, and it is the sentence a caller reads first. It
  does state the `retry_safe` pairing, but two sentences later and never
  connected to that instruction.

The trap is that both situations surface as a timeout with near-identical text,
and the correct actions are opposites. The discriminator is already in the
response; the description just does not point at it. Minimal wording change:

> If the create ACK is lost -- `retry_safe=false` with an `operation_id` -- call
> this tool again with that `operation_id`, `client_id` and `owner_id` to
> reconcile the same operation without dispatching another create. When a
> response instead says `retry_safe=true`, create was not dispatched: retry
> plainly with no `operation_id`, because a recovery read can only report the
> operation as missing.

Per `AGENTS.md` §2 this touches caller guidance, so the same discriminator
sentence goes into both README tool tables and both packaged Skills, followed by
`python -m scripts.check_tool_docs`. `browsertap-default/SKILL.md` currently
covers only the `ready=false` case (`:41`); `browsertap-bridge-recovery` the same
(`:175`). Neither mentions `retry_safe` at all, which is why a caller reading
only the Skill has no way to make this distinction.

## Ownership

`server.py`, both READMEs and both Skills are the implementing session's files.
Nothing above is applied. Sequence if it is taken up: patch 2 first and watch it
fail, then patch 1, then the three mutations on a copy, then patch 3 plus
`check_tool_docs`. Patch 1 alone changes a response shape with no reader
asserting it, which is how the branch got here in the first place.
