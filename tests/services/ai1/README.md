# AI-1 synthetic engineering delivery

This directory accompanies the local AI-1 parent-explanation integration with
LMS. The implementation is based on DeepTutor v1.7.1 at
`f7aed0744c36c5afef16a4261c31ebee1208cac2`.

## Scope and safety

The integration adds a narrowly authenticated internal operation API, durable
SQLite operations, single-claim sending, terminal replay, query-only recovery,
and a restricted completion provider. It is mounted in the existing application;
it does not add an agent or an independently deployed service.

No runtime configuration means off. The default provider has no qualified input
counter, so real enabled startup is rejected. Synthetic counters and mocked
provider responses validate engineering behavior only. Deferring the technical
counting evidence does not authorize real sending or remove this prerequisite.

This delivery does not authorize model calls, production secrets, deployment,
upstream submission, or closure of the AI-1 gate. Unknown outcomes retain their
original operation identity and reservation; they must not trigger a new send.
Model output remains a parent-only draft requiring human checking and correction
before approval. No textbook, private media, or real child data is included.

## Local validation

With the existing project dependencies and pytest installed, use an isolated
temporary HOME and DEEPTUTOR_HOME and run from this repository root:

```sh
python -B -m pytest -p no:cacheprovider tests/services/ai1 -q
```

The 2026-11-02 delivery check used the unmodified root fixtures and temporary
storage, with no inherited model credentials: 166 passed, 3 warnings. This is
not a claim that the entire DeepTutor suite or real provider acceptance passed.
The existing CI may run broader checks; their results must be reported separately.

The `lms_*acceptance.py` and `node_owned_group.cjs` helpers are used by LMS formal
HTTP/browser synthetic validation. They are not deployment or real-send tools.

## Cross-repository evidence

The companion LMS delivery branch is `codex/ai1-synthetic-delivery` in `mflyn/LMS`.
Its `docs/development/family-ai1-test-plan.md` section 17 and
`docs/development/family-self-directed-learning-mvp-gate.md` section 29 distinguish
historical failures, synthetic acceptance, and pending real-model/human checks.
The frozen matrix lists the original source fingerprints; this README adds no
business logic and does not rewrite those evidence snapshots.

B/D/AI-1 remain OPEN. DEVICE_PENDING, historical Docker INCOMPLETE_FAILED,
T4/T8 OPEN, T9 NOT_STARTED, T5-NB-001/002 OPEN_NONBLOCKING, and advanced recovery /
new orphan collection REMAINDER_DEFERRED are not closed by this delivery.
