# Configuration/diagnostics repair design

Use the shared frozen source as the implementation baseline. Keep normalization
at the relevant boundary: validate the configured port before any bind/spawn,
pin configured filesystem paths in the child's environment before cwd changes,
and report actual filesystem read states without exposing token contents.

Network probes and listener binding must agree on address family. Doctor's
supplementary reachability probes cannot erase a completed diagnostic payload.
Existing structured bridge errors remain distinguishable from malformed remote
payloads. Limit validation to contract fields actually guaranteed by the local
diagnosis producer; avoid inventing a schema that rejects valid old outcomes.

Connection age represents transport/lifetime stability. Track traffic activity
separately for HTTP expiry and keep the age stable across poll/tab snapshots.
Use explicit new transport and actual lifetime transitions to reset age.

The package's existing public contracts and ownership rules apply. Do not
weaken assertions to hide errors. New reports must bind this slice's files to
the seeded baseline, not mix the seed patch into the returned change.
