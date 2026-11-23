# AI-3 synthetic engineering delivery

Date: 2026-11-05. Base: `a5e64ca49667ddba2a0ba80c02ac1c59959dc2f9`.
This delivery contains 14 frozen implementation/test files plus this note.
It targets the owner's fork only. It does not authorize deployment, upstream
publication, real model requests, or changes to an existing running service.

The scoped operation supports AI-3 requests, shared-profile accounting contracts,
durable claim/query recovery, terminal replay and bounded JSON requests. Existing
AI-1 operation behavior and the default-off boundary remain protected by tests.
The leadership cancellation correction preserves parent cancellation while
stopping child services.

## Verification and historical failure

The unchanged candidate previously passed 242 independent synthetic tests.
A subsequent delivery check emitted five failure markers then hit its existing
180-second outer timeout; it did not produce a complete failure summary. Its
temporary raw logs were unavailable after a host restart. Do not infer a root
cause or invent a recovered log/hash.

On 2026-11-05, the same test collection passed 242 tests in the first-failure
diagnostic (17.51 seconds) and again with original quiet parameters (13.38 seconds).
Both runs exited zero with four warnings, using the original 180-second outer
timeout, existing AnyIO fixture, isolated HOME and loopback-only network guard.
Provider behavior is synthetic. Test timeouts/assertions and product code were
not changed for the rerun. No test interpreter remained afterward.

The current candidate passes these checks; the historical timeout remains
unreproduced and unexplained, not silently resolved. Keep the PR in Draft for CI
and review. Earlier Ruff checks passed; local Ruff 0.15.7 differs from CI 0.16.0.

LMS carries the paired file hashes, original/public evidence identities and
acceptance records in `docs/development/evidence/ai3-integration-20261105/`.
AI-3 acceptance is synthetic engineering only. T4/T8/AI-1 OPEN, DEVICE_PENDING,
Docker INCOMPLETE_FAILED, qualified-counter pending, historical failures,
nonblocking followups and deferred recovery/cleanup scope remain unchanged.
