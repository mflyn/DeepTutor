# Manual trial v2: local Provider and runtime batch

Status: LOCAL_SYNTHETIC_READY_FOR_REVIEW / INDEPENDENT_REVIEW_PASSED / USER_BATCH_ACCEPTANCE_PENDING

## Approved scope and pre-implementation self-review

The user approved the LMS manual-trial design on 2026-11-21 and accepted its
first local ledger/contract batch. This batch implements only the matching
DeepTutor restricted v2 envelope, Provider and formal runtime. It does not
enable real sending, alter existing settings, deploy or call a model.

Reuse the existing parser, single-shot transport, durable operation journal and
formal main lifecycle. Preserve v1 qualified-counter rejection and all historical
snapshots. V2 must be explicit in both the request and server profile; the profile
also requires explicit estimate acknowledgement, never inferred from a key or
an enabled flag. Estimates are not single-call or experiment hard cost caps.

Plan: first add failing v2 tests; extend closed contract and persistent public
projection; bind v2 Provider preparation to exact full wire bytes (32768 maximum),
4096 output and one attempt; exercise formal main with synthetic configuration,
no credentials fallback and no network. LMS remains the shared quota/halt owner.
The remote service must not invent a second ledger or accept client quota claims.

Self-review: preserve namespace checks, same-ID body conflict, historical accepted
read-only replay, UNKNOWN no resend, and current policy revalidation immediately
before claim and transport writes. Query-only must read both versions without
current pricing or counter. New trial configuration must not make v1 sendable.
Runtime defaults remain off. Rollback after v2 records means stopping new sending
and retaining query-capable code, never deleting records or downgrading their
schema. No changes to the live Ubuntu instance are authorized.

Exact input qualification remains deferred, not solved. Local SDK tests use an
HTTP transport substitute and synthetic secrets. Historical failures, pending
device acceptance and all other unclosed/deferred gates remain unchanged.

## Implemented protocol and recovery boundary

Both `ai1-provider-operation-v2` and `ai3-provider-operation-v2` require explicit
`feeMode=estimate_only`, `estimateMethodVersion=reference-8192-4096-v1`, reference
input/output values 8192/4096, `estimatedReservationMicros` and
`feeNoticeVersion=estimate-not-cap-v1`. `maxRequestBytes=32768` replaces the v1
input-token claim. The server profile additionally requires `sourceReference`
and boolean `estimateAcknowledged=true`. These are local configuration
requirements, not proof of pricing eligibility, exact counting or authorization
to send a real request. No actual configuration was changed.

V2 preserves complete usage even above the input estimate; missing usage stays
null through persistence and query-only restart. It does not cap or manufacture
the actual charge. Shared quota, settlement and subsequent send inhibition remain
LMS responsibilities. Off is still the default. V1 still requires its qualified
counter and cannot silently become v2. The final prepared canonical bytes,
deadline, namespace and original operation identity remain guarded.

Independent review found formal AI1 could dispatch historical v1 accepted under
a trial profile and change its terminal state. The formal runtime now disables
replay dispatch, as AI3 already did; new records still dispatch once. The
permanent formal-main regression checks zero dispatch, identical projection and
unchanged SQLite bytes. The synthetic factory default remains compatible.
Local red/green evidence is retained; user closure of this new P2 remains pending.

## Historical and resumed verification

The 2026-11-21/22 batches remain separate: initial contract/plugin and fixture
failures; 21 new tests passed; full run 283 passed plus five original leader
tests failed for missing pytest_asyncio; expanded 27 passed; pre-P2 services
289 passed; then P2 red/green. The final 180-second timeout, partial FFF output,
30-second formal subprocess timeout and incomplete freeze remain preserved in
`ai-manual-trial-deeptutor-batch-b-20261122`. They are not retrospectively green.

On 2026-11-23 all 17 source hashes initially matched that snapshot. Read-only Git
and file access responded normally. Instrumented synthetic settings reads took
milliseconds; unrestricted-module import then failed on absent optional
defusedxml, not a timeout. The existing core/server formal default-off test passed
in 4.4 seconds. The cause of the historical stall cannot be established from the
available stack alone; no business fix, system change or installation is claimed.

The first resumed services run had 288 passed / two failures because the new
diagnostic network guard blocked the original loopback HTTP fixtures. Only the
diagnostic guard was corrected to allow live listeners bound by that test
process; external and other endpoints remain denied. The unchanged services
then passed 290. After review-driven test protection additions, the final related
suite passed 298 / zero failures or skips in 77.05 seconds (77.966 seconds wall).
It includes 36 v2 tests; it excludes the five unchanged leader tests whose prior
missing-plugin failures remain recorded, and is not an all-repository green run.

New protection reuses existing formal-main and actual httpcore fixtures: v2
cross-namespace same-ID rejection, one-attempt 429 followed by durable unknown,
unknown usage after restart, near-limit Unicode/escaped wire with content-length
and output limit assertions, and connect/TLS/body deadline checks. No old
assertion, test timeout, resource budget or retry limit was relaxed.

Formal-main tests use ASGI TestClient and synthetic settings; SDK success/failure
uses MockTransport, and httpcore trace tests substitute only network I/O. Two
original tests additionally use real local loopback HTTP with a synthetic
Provider. None proves real LMS-to-DeepTutor-to-model, Ubuntu or device acceptance.
The fresh freeze records a full patch from d5 and an incremental patch from the
old d1 formal snapshot, including separately identified previously approved
follow-up documentation. Neither patch is the old d1 patch or a new Git commit.

## Self-review and independent review

The eight in-memory mutation categories produced 12 expected assertion failures,
seven passing controls, zero collection/runtime errors, zero skips and no timeout:
historical replay dispatch, missing estimate acknowledgement, raw-body bound,
incorrectly applying the v1 usage cap, Provider unknown-usage zero fabrication,
pre-key validation removal, final HTTP write deadline removal, and store return
projection zero fabrication. The last mutation changes the returned projection,
not SQLite persisted bytes. The usage-cap mutation rejects, rather than truncates,
an otherwise valid usage value. Source files were not mutated on disk.

Independent review ran 74 cases: 36 new v2, 12 existing AI3 formal runtime and
26 existing AI1 runtime, all passing without errors or skips. JUnit was 69.856
seconds, wall 70.758 seconds, with the original 180/30-second budgets. Twenty
candidate and related configuration/test hashes were unchanged before/after.
The reviewer audited the eight mutation runs without rerunning the mutations;
their unmutated controls passed independently. No remaining P1/P2 was found in
this local batch. The historical-accepted P2 is technically reverified, but its
user closure and this batch's acceptance remain pending.

Independent evidence manifest: `c16899fec94236146aaaa86ca1100d05eae879ea09b25cc76ae183e45095336d`.
The final local archive `ai-manual-trial-deeptutor-final-20261123` binds source,
full/incremental reconstruction, separate development and independent evidence.
Historical startup cause remains unconfirmed; successful current runs do not
erase it, the five plugin failures, or the optional-module diagnostic failure.
No counter qualification, real sending authorization, production installation,
server update, total gate closure or device acceptance is implied.

## Subsequent user acceptance (2026-11-23)

Current batch status: ACCEPTED / USER_APPROVED / LOCAL_SYNTHETIC_ONLY.
Historical-v1-accepted replay P2: CLOSED / USER_APPROVED.
The pending descriptions above are the original review-time snapshot and remain
unchanged. The user subsequently approved this batch and the replay P2 closure;
this addition records that approval, not a new implementation or test run.

Accepted identity: base `d5a30cbd95c7fbed53f7423dd046915a0eb1ebf6`, original
17-file full patch SHA-256
`72edd3e34816811e8da724afa4a097325060b258a996fd7adff7bd8126229aa6`,
increment from the old formal-runtime snapshot
`3c39822fa1fab97749f94e2d942155fd595f33516189462b73c3fb2fe9df81d6`.
Original 84-file manifest:
`0e42015d3a1fddaab6ce71ccfff06e645ccf713ac07c9b329139d84a41a5d27b`.
Independent evidence manifest:
`c16899fec94236146aaaa86ca1100d05eae879ea09b25cc76ae183e45095336d`.
The corresponding LMS approval record is
`docs/development/evidence/ai-manual-trial-deeptutor-acceptance-20261123/README.md`.

Development 298, independent 74, and 12 expected mutation failures / seven
controls remain separate results. The reviewer did not rerun the mutations.
The earlier Ubuntu 267 cases exercised the old formal-runtime patch, not this
v2 delivery. The five historical leader/plugin failures and other failures
remain preserved; no whole-repository all-green claim is made.

This appended document has a new delivery hash. The other 16 frozen files and
the original patch/archive hashes retain their original identities. A new
delivery patch must include this addition without relabelling the old freeze.
The LMS page batch has its own later approval and evidence; it is not part of
the 298/74 counts. Real sending is not authorized, estimates are not hard cost
caps, exact input qualification remains deferred and v1 remains fail-closed.
Default-off, UNKNOWN no-resend and original-command protections are unchanged.
T4/T8/AI-1 OPEN, DEVICE_PENDING, SERVER_TIMING_UNVERIFIED,
Docker INCOMPLETE_FAILED, historical failures and other deferred states remain.
PR #2 remains Draft with Actions blocking deferred (support ticket 5036799);
local acceptance does not imply CI success, merge, server update or deployment.
