# Coverage map for the refund and checkout workflows

This page maps the acceptance criteria and open decisions in [aeoess/agent-governance-vocabulary#193](https://github.com/aeoess/agent-governance-vocabulary/issues/193) to what this runner executes today, and states what a connected run still needs. Pins are the ones in [`components.lock.json`](../src/checkout_runner/components.lock.json) at v0.1.0.

## Who owns what

| Role | Owner | What the runner does with it |
| --- | --- | --- |
| Merchant decision mapping, admission, consumption, admission signing | APS (aeoess) | Runs the exact-refund candidate's own cases R0 to R9 at `c31d94a`. Drives `aps-checkout-gate` at `dedc818` end to end as the admission gate in the checkout matrix. |
| Signed approval fixtures and verification | PIC (madeinplutofabio) | Runs the 22 signed refund approval cases, both integrity probes and the self-check at `eb7cd46`. |
| Execution: dispatch, deduplication, reconciliation, receipts | Frequency Conduit (altrudev), source private | Not driven. The runner's reference executor stands at that boundary in the checkout matrix and is replaceable by any executor that implements the frozen handoff. |
| Independent downstream reader | nutstrut/default-settlement-verifier, pin pending | Gets the sealed bundle through `--reader`; its result is kept apart and decides nothing in the runner. |
| Refund predicates C1 to C3 | Rul1an/assay | Runs the C1, C2 and C3 readers and reproducers and the offline baseline and mutation verifiers at `c039b7c`. |
| Combined runner and admission-signature verification | this repository | Fetches every pin, runs the matrix and suites, checks each signed admission handoff independently of its producer, retains every stream, seals the bundle. |

## The sandbox acceptance criteria

| Criterion in #193 | Executed today |
| --- | --- |
| One approved execution that matches the signed approval exactly | Matrix `match_dispatch`; APS R0; PIC allowed case; refund-handoff vector 01 |
| Zero executions for an altered payment, amount or currency | Matrix `changed_amount`, `changed_merchant`, `changed_currency`, `changed_agent`, `changed_items`; APS R1 to R4; PIC argument cases; refund-handoff vector 04 |
| Zero executions for expired, missing or unverifiable approvals | Matrix `expired_permit`, `forged_signature`, `tampered_evidence`, `untrusted_signer`, `refusal_as_handoff`; APS R5, R7; refund-handoff vectors 13, 14, 16 to 18 |
| No second admission for a consumed approval | Matrix `replay_after_success`, `replay_after_expiry`, `gate_replay`; APS R6; refund-handoff vectors 08, 09 |
| A lost response retried under the same idempotency key returns the original result | Matrix `lost_response_retry`, `provider_crash_before_commit`, `lost_response_reconciled`; refund-handoff vectors 10 to 12; C2 |
| A report cannot use an observation made after its cutoff | Matrix `lost_response_after_cutoff`; refund-handoff vectors 15, 21; C3 |

## The handoff decisions proposed in #193

| Decision | Where the runner stands |
| --- | --- |
| Committed admission record, not a free-standing reference | The checkout matrix refuses any handoff whose committed evidence does not re-derive (`decision_ref_mismatch`). The refund-handoff/0.1 suite covers a signed admission record. |
| Scoped replay identity | The idempotency key binds the trusted gate, the permit and the provider namespace. Same key, same operation. |
| Crash-safe lifecycle | `dispatch_pending` is durable before the provider call. Executor crash, provider crash before commit, lost response and gate crash are separate cases. |
| Closed canonical contract | The refund-handoff/0.1 corpus runs as the current candidate, with strict canonical JSON and unknown-field refusal; the runner re-pins to whatever the group freezes. |
| Key and time semantics | Trust comes from the executor's configuration; expiry refuses new dispatches; finished operations are returned without extending validity. |
| Independent evidence and limitations | Separate streams, ESTABLISHED / CONTRADICTED / NOT_ESTABLISHED per claim, report cutoff, sealed bundle, re-derivation on verify. |

## What a connected refund run needs

A connected result will not be reported until each of these is public and pinned:

1. frozen signed admission and handoff schemas, with the signature domain and signed field set;
2. a trust-anchor snapshot naming each test key, its role and its validity;
3. producer-emitted fixtures: the APS admission record and handoff, the matching PIC-signed approval, and execution records from Conduit or another executor;
4. the report cutoff;
5. each producer's command line, so the runner invokes the producer's code rather than re-implementing it.

The runner then drives those producers in one run, verifies each signed admission handoff and committed record itself, retains the streams separately, and hands the bundle to the reader.

## What this is not

Every record is synthetic and the provider is a local simulator. A run by this repository's author is a fixture run, not an independent run or a host adoption. The refund-handoff/0.1 profile is a proposal, not the group's accepted contract. Passing the component suites side by side is not a connected result.
