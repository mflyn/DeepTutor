# AI3 formal runtime follow-up (2026-11-21)

Base: `d5a30cbd95c7fbed53f7423dd046915a0eb1ebf6`. Local synthetic work only.
This follow-up is separate from LMS documentation PRs. No commits, deployment,
real credentials, model calls, database migration or gate closure are authorized.

## Findings and minimal plan, recorded before implementation

The formal main lifecycle only mounts AI1. AI3 has a standalone synthetic ASGI
factory, not a production lifecycle. The default restricted Provider has no
qualified counter; enabled startup must continue to reject that condition.

1. Reuse the private AI1 configuration loader, journal and narrow HTTP contract.
   Add `DEEPTUTOR_AI3_CONFIG_FILE`, independently defaulting to off, using the same
   closed configuration keys. Mount `/internal/deeptutor/ai3` before the broader
   AI1 mount. Start both before application readiness and stop both on all exits.
2. Bind AI3 to its existing request parser, service and schema. Keep the existing
   `x-ai1-service-token` wire header; AI3 has its separately configured token.
   Preserve AI1 behavior and AI3 `dispatch_replays=False`. Do not initialize stores
   during startup. Query-only and store-isolated must not require Provider access.
3. Reuse the restricted SDK byte/transport path through an AI3 parser-specific
   subclass. No counter is installed or selected from settings. Synthetic injected
   counters/transport exercise serialization only, not real qualification.
4. Test actual main lifecycle, authentication, profile namespace, terminal/accepted
   replay, query-only restart, startup failure cleanup and exact request bytes.
   Freeze base + patch + file hashes after tests and self-review.

## Self-review before implementation

The narrower mount must precede AI1 to avoid route shadowing. A failed AI3 startup
must close already-started AI1; unrelated main startup failure must close both.
Shared journal IDs are protected by the existing schema/hash checks. Wrong request
schemas must not write or dispatch. Historical accepted AI3 replay must remain
read-only even after process restart; UNKNOWN cannot acquire a new send claim.
No chat Provider, environment credential fallback, retry or new network endpoint
is introduced. Rollback is reverting this local patch with both features off;
there is no schema change or real data operation to undo.

## Counter qualification remains blocked

Existing LMS AI1 test plan retains QUALIFIED_COUNTER_PENDING: no accepted evidence
binds hosted model/profile to tokenizer plus complete system/user roles, generation
prefix and any additional billable protocol overhead. A byte-bound or synthetic
8192 result is not such proof. Existing official-inquiry/history is not replaced.
This batch can verify missing/mismatched/expired synthetic qualification rejection,
8192/8193 boundaries, Unicode/escaping, changed prepared requests and zero transport.
It cannot certify a real counter, price, cost cap or enabled deployment. Production
counter installation and actual model execution remain blocked pending reviewable
model-specific evidence and a separate activation decision.

## Implemented candidate and local validation

The formal `deeptutor.api.main` now mounts AI3 before AI1 and manages both runtimes
in its actual lifespan. AI3 reuses the private configuration keys `mode`,
`storePath`, `storeContinuous`, `serviceTokenFile`, `providerKeyFile`, and `profile`.
An absent `DEEPTUTOR_AI3_CONFIG_FILE` leaves AI3 off. No configuration file, service
credential, operation database or qualified counter is created by this patch.
The current default Provider rejects enabled startup without a qualified counter.
Existing server configuration and data have not been inspected or modified.

Tests enter actual main/lifespan in isolated processes with synthetic files and
network connections denied. Enabled successful paths inject a Provider substitute;
counter tests inject synthetic counters and HTTP transports. Neither proves real
model qualification, remote deployment, or real generation acceptance.

- Initial product red: actual main lacked `ai3_runtime`; one test failed as expected.
  Earlier collection/import failures are also retained, not relabeled product reds.
- Formal runtime plus Provider tests: 26 passed in 27.30 seconds before the review
  improvement, then 26 passed in 25.28 seconds after it (one existing warning each).
- Original AI1/AI3/leader regression plus the new tests: 262 passed and five failed
  in 41.87 seconds (42.870 seconds outer duration, unchanged 180-second budget).
  All five failures are leader async tests: the available local interpreter lacks
  `pytest-asyncio`. Those tests were not skipped, rewritten, installed around or
  counted as passes. This run precedes the final negative-test improvement; the
  26-test rerun above validates that improvement, not a new full-green run.
- Independent static review found a false-positive possibility in the missing
  service-credential test: its thrown sentinel could be translated to the expected
  startup error. The revised test records calls at the store constructor and asserts
  zero calls outside the exception handler. Production ordering did not change.
- Three isolated-copy mutations were killed: moving store construction before
  credential validation (one failure), enabling dispatch of historical AI3 accepted
  replays (one failure, two unchanged cases pass), and removing prepared-request
  revalidation (four failures). Original assertions/timeouts remain intact.
- Ruff passed on the nine affected service/test files; `git diff --check` passed.

Independent follow-up code review found the negative-test issue fixed and no new
actionable code finding. Its first execution was diagnostic: 163 passed / five
async-plugin failures in 35.94 seconds, with command-line `addopts` overriding
`--strict-config`. That result is retained but is not original-configuration
acceptance and must not be combined with the development counts above. Missing
dependency failures and any subsequent original-configuration verification are
recorded separately in the frozen validation evidence.

The independent original-configuration check subsequently ran once with every
repository `addopts` retained: 163 passed / five async-plugin failures, zero skipped,
one warning, exit 1, 35.66 seconds (36.070 seconds outer duration). All ten code/test
file hashes were unchanged. This final documentation append records the returned
review results; it is not an additional code revision or test execution. The review
identified no new actionable code defect, but explicitly did not approve a gate
closure or real counter. Neither failed execution is superseded or counted as green.

The formal assembly is a local review candidate, not a server acceptance. The
missing local async test dependency and real counter qualification remain explicit
limitations. No AI1, AI3, device, deployment or other historical gate is closed here.

## Subsequent authorized Ubuntu validation (2026-11-21)

This is a later record supplied by the testing/review thread and checked locally;
the earlier authorization boundaries and failed local runs above remain historical
facts. This development follow-up made no server connection or deployment.

- Following separate user authorization, the existing test instance was updated at
  16:41:37 Asia/Shanghai to base `d5a30cbd95c7fbed53f7423dd046915a0eb1ebf6` plus the
  11-file patch `d1dc53b249e590c6d48f03ba1b191efe6c5ba65acacee4898e663b8e1b410ade`.
  This is not a new Git commit. Image `local/deeptutor:1.7.1-ai3-formal-d1dc53b2`
  has ID `sha256:141e5f5b9739b15f323f9588fdf571a0d2d1c97262e55f3189c0a88cc6f25b77`.
- The frozen 4506-file source digest is
  `e45bf2deb3f4b44843c8fd301ee60de00a4f733b0ac67f577be63e7b5a4f075b`.
  Online checks matched 1485 runtime files, six startup files and 3632 unchanged
  frontend files. Frontend artifacts were verified and reused, not rebuilt.
- Original configuration tests ran against actual `/app` code in that image, with
  synthetic HOME/data, network disabled and no existing data mounts. Original 241
  plus formal-main 12 plus Provider 14: **267 passed, zero failures/errors/skips**.
  JUnit time was 95.680 seconds; phase duration was 98.518 seconds. The original
  180-second outer and 30-second formal-child budgets were unchanged. Python was
  3.11.18, SDK 2.54.0, pytest 8.4.2 and pytest-asyncio 1.2.0.
- This is distinct from local `review-r1` (267 passes; four mutation classes with
  seven expected reds/two controls), the development 262/5 result and earlier
  Ubuntu 241 passes. None is relabeled or used to erase a previous failure.
- Live/ready/login returned 200. AI1/AI3 GET/PUT remained 503 `AI_DISABLED` with
  `no-store`. Assembly evidence is the online source identity plus same-image
  formal-main tests, not the generic off response alone.
- The report verifies 16 settings/credential-file hashes, nine SQLite schema/table
  counts and 86 historical llm_calls preserved; other ten LMS services unchanged;
  temporary test containers zero. This is not a supplier billing audit.
- Subsequent zero-call preflight stopped at `STOPPED_QUALIFIED_COUNTER_PENDING`.
  Candidate-not-deployed is no longer a blocker for this exact patch. Real service
  credential/profile/store/address admission was not performed; zero real model
  calls and default off remain. Formal assembly is **Ubuntu limited synthetic
  validation passed**, not real LMS-to-DeepTutor-to-DeepSeek, counter or device
  acceptance. This record does not alter total gates or user approval states.

Evidence locator in the testing thread: `ai3-runtime-counter-followup-2026-11-21/`:

| Frozen package | Files | manifest.json SHA256 |
| --- | ---: | --- |
| `ubuntu-r2` | 33 | `d60949bb564b0ce37d62ecc5e37061c53caf48aeb9847e0a11da29399135f25c` |
| `review-r1` | 42 | `53590383ca408390b3d75ee631d2d0a5324a50e803c5745a33970b2eb0776b36` |

Both manifests and every listed file were checked locally against the handoff.
Read `REPORT.md`, `verification.json`, `candidate.json`, `preflight.json`, and
`remote/reports/{switch,tests,finish,environment}.json` plus `ai-tests.xml` in
`ubuntu-r2`. Original development 31-file archive and the older 29/52/9 packages
are immutable; this append changes the current documentation, not those snapshots
or the identity of the deployed d1dc53b2 patch.

## Public counter basis recheck and minimal decision (2026-11-21)

Scope remains the existing two-message `system`/`user` text contract, non-thinking,
no tools, one attempt, input ceiling 8192 and output ceiling 4096. The Provider
counts its canonical SDK wire bytes and revalidates before sending. Wire-byte
identity is necessary, but JSON wire bytes are not themselves the model prompt
tokenization or proof of billable upper bound. No real runtime profile/key was read.

Public first-party sources read in this follow-up:

| Source | What it establishes | Remaining gap |
| --- | --- | --- |
| [Models and pricing](https://api-docs.deepseek.com/quick_start/pricing/) | API name `deepseek-v4-pro` currently maps to `DeepSeek-V4-Pro-0813`; billing uses input/output tokens | No immutable tokenizer/encoding revision or extra-input bound is identified there; no live quote/profile is updated |
| [Token usage](https://api-docs.deepseek.com/quick_start/token_usage/) | Character ratios are approximate; a demo tokenizer is offered; returned usage is authoritative | Not a guaranteed bound for this complete hosted request, nor a version binding of the demo to 0813 |
| [Official model card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro) | Public model provides separate message encoding followed by tokenization | Public weights/encoding are not explicitly bound to hosted 0813 in the reviewed material |
| [Fixed encoding documentation](https://huggingface.co/deepseek-ai/DeepSeek-V4-Pro/raw/b5968e9190ef611bbf34a7229255be88a0e937c1/encoding/README.md) | Documents BOS, roles and non-thinking assistant prefix; demonstrates why body-only counting is insufficient | This is a fixed open-model reference, not a hosted billing guarantee |
| [Chat completions](https://api-docs.deepseek.com/api/create-chat-completion/) and [thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/) | Describe usage fields and explicit `thinking.type=disabled` | No pre-send count formula or hosted overhead upper bound was found in these reviewed pages |

The fixed public repository revision is
`b5968e9190ef611bbf34a7229255be88a0e937c1` (its commit page identifies a June 22
technical-report update), while the API identifies the served Pro version as 0813.
This does not prove their tokenizers differ; it means equality/applicability has
not been established. No downloaded model/code was executed, no packages installed,
and no authenticated API/model requests or new official inquiry were sent.

### Minimal alternatives and recommendation

1. Exact local counter: requires an official binding from hosted 0813 to fixed
   tokenizer and complete non-thinking message encoding, including billable server
   additions. Once obtained, pin data/hash, apply only to the existing narrow
   profile, reuse prepare/send validation and add known vectors and negative tests.
2. Conservative bound: acceptable only with a proof covering all transformations.
   A byte-level BPE bound on the *fully rendered prompt* can be considered if the
   tokenizer/preprocessing and server additions are bounded. Counting only JSON or
   adding an arbitrary margin does not prove coverage. No such complete bound was
   established in the reviewed sources.
3. Keep fail-closed while obtaining the missing binding or a documented conservative
   overhead bound. **Selected for this follow-up.** Do not change the model, token
   ceilings, costs, retries, original operation IDs or unknown-result handling.

The smallest useful external clarification is: for the existing text-only two-role
request to `deepseek-v4-pro` / 0813 with thinking disabled and no tools, which fixed
tokenizer/encoding revision governs billed prompt tokens; does it include BOS,
role separators and assistant prefix; and what, if any, additional billable input
is inserted, with what bound and version-validity rule? A few observed usages or
successful chat calls would not prove an all-input upper bound. Any authenticated
probe, new correspondence or deployment requires separate authorization.

### Self-review before any counter implementation

The assembly evidence is sufficient for the limited recorded conclusion. Counter
evidence is **insufficient for production qualification**, so implementation and
production injection stop here. Existing counter-missing/version/8192/Unicode/
changed-input rejection tests remain unchanged; no new counter test is presented
as proof of an implementation that does not exist. Unknown outcomes still retain
their claim and original ID. Default off and `QUALIFIED_COUNTER_PENDING` remain.
This follow-up is documentation and evidence only; rollback is removal of this
new append, not changes to deployed data or any historical frozen package.

## Approved record and counter-basis review (2026-11-21)

The user approved this batch's limited assembly acceptance record and counter-basis
review after the read-only review found no new required correction. Approval is
limited to the accuracy of these records and the evidence-gap decision, not counter
qualification, real sending, a total gate closure or deployment authorization.

| Identity or evidence | Binding retained by this approval |
| --- | --- |
| Git base | `d5a30cbd95c7fbed53f7423dd046915a0eb1ebf6` |
| Deployed and Ubuntu-tested patch | `d1dc53b249e590c6d48f03ba1b191efe6c5ba65acacee4898e663b8e1b410ade` |
| Reviewed document-follow-up full patch | `3e1a8f0191ccf8c6f797181b9135600f9f98e26ecd9e18587bdf342cfd2dc8bb` |
| Reviewed document SHA256 before this approval append | `250059acea3c7b938ac15d420e7167173ce80919ee360915b5db22fae343f76e` |
| Ubuntu `ubuntu-r2` manifest | `d60949bb564b0ce37d62ecc5e37061c53caf48aeb9847e0a11da29399135f25c` |
| Independent `review-r1` manifest | `53590383ca408390b3d75ee631d2d0a5324a50e803c5745a33970b2eb0776b36` |

The approved Ubuntu result is 267 passed / zero failures, errors or skips, JUnit
95.680 seconds, phase 98.518 seconds, under the original 180-second outer budget.
The review checked the original command, actual `/app` source identity, structured
JUnit counts and record consistency; it did not rerun the product suite or connect
to the server. The older 262/5 and 163/5 local results, diagnostic configuration
override, historical failures and separate mutation counts remain unchanged.

This approval append changes the current document after the reviewed freeze.
Neither old full patch nor old manifest is regenerated or assigned to these new
bytes. Ten code/test files remain unchanged. T4/T8/AI-1 OPEN, DEVICE_PENDING,
SERVER_TIMING_UNVERIFIED and other unclosed/deferred states are not changed by
this record. `QUALIFIED_COUNTER_PENDING` and default-off/fail-closed remain.

## Minimal official clarification draft - awaiting user confirmation

Status: draft only; not sent. No new email, authenticated API probe or paid request
is authorized by approval of the records above. No keys, account identifiers,
children's data or private deployment details are needed in this inquiry.

Subject: Complete billed input-token bound for deepseek-v4-pro / 0813

We need a pre-send input-token count or provable conservative upper bound for a
single text-only OpenAI-format `POST /chat/completions` request: model
`deepseek-v4-pro`, exactly one system string and one user string,
`thinking: {"type":"disabled"}`, `stream:false`, `max_tokens:4096`, with no tools,
images, files, prior turns or optional features. Our local admission limit is
8192 input tokens; this is our limit, not an assumed actual input count.

1. Which fixed tokenizer and message-encoding version applies to billed prompt
   tokens for hosted DeepSeek-V4-Pro-0813? Does public revision
   `b5968e9190ef611bbf34a7229255be88a0e937c1` apply? Please provide immutable source
   references/hashes or the correct version and the conditions under which it is
   valid, including how a hosted-model revision change can be detected.
2. For this narrow request, does that encoding cover every billed input token,
   including BOS, system/user role boundaries and the non-thinking assistant
   generation prefix? Are normalization, escaping, default prompts or other
   server-added input included? If anything is additional, please specify its
   exact count or guaranteed maximum and applicability conditions, including
   Unicode and strings resembling special-token delimiters.
3. Can you provide a documented offline counting procedure or conservative
   upper-bound formula covering the above, with a few complete-request reference
   vectors and expected counts? We need a bound valid before sending, rather than
   an average character ratio or only post-request usage. If neither is available
   for the hosted service, please explicitly confirm that limitation.

Reference vectors help validate an implementation but alone do not prove a bound
for all accepted inputs. A reply must still be reviewed for version/encoding and
billable-input coverage before implementation or injection. If those facts remain
missing, real enabling stays rejected; no arbitrary margin, fixed 8192 counter,
new UUID retry or refund of UNKNOWN is introduced. Sending this draft and any
subsequent model/server operation require separate user authorization.
