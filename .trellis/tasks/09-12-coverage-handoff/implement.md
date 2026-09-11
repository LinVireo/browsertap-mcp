# Coverage integration steps

1. Read exact claims, the peer readiness note, validation contracts, and patch
   manifest. Confirm the interpreter imports the original checkout.
2. Snapshot the 18 original test files and 11 frozen setup-owned identities.
   Require the test paths to have no pre-existing unclaimed edits.
3. Recheck and apply the patch to the original tree. Adapt only owned tests if
   current diagnostics contracts require it.
4. Run the 18 affected test files together with a JUnit report under
   `out/coverage-handoff-20260912/integration/`, using the original checkout's
   Python. Run Ruff for tests, diff checks, and LF checks for changed tests.
   Do not run live, write sealed artifacts, or repeat the candidate's tests.
5. Freeze the resulting test hashes. Dispatch an independent Trellis checker;
   resolve justified findings in the same scope and verify changed behavior.
6. Send a new readiness file naming evidence and frozen test hashes. The setup
   owner performs whole-tree coverage and packaging. Reconcile source identity
   before citing those results; keep the task open until their status is known.

Project `SPEC.md` rev7 remains `delivered_stub` for pre-existing live/manual
requirements. Lifecycle analysis blocks release only; this integration does not
rewrite SPEC, record a delivery baseline, or claim full project delivery.
