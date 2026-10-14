# Claims and the bundle

Every claim is derived by `checkout_runner.report.derive` from one case directory's files and nothing else, so a reader that copies the bundle gets the same answer. ESTABLISHED means the retained streams show it; CONTRADICTED means they show the opposite; NOT_ESTABLISHED means they do not decide it.

## Claims per case

| Claim | Reads | ESTABLISHED when |
| --- | --- | --- |
| `admission.decisions_verify` | decisions | Every decision the gate returned passes this runner's own check of the signed receipt and its committed evidence, and the gate's own verifier agrees |
| `admission.expected_decisions` | decisions | The verdicts and reasons equal the case's declared list |
| `dispatch.expected_attempts` | attempts | The executor's attempt results equal the declared list |
| `dispatch.provider_calls_bound` | attempts, decisions | Every attempt that reached the provider stands on a verified permit for exactly the dispatched request, before the permit's `valid_until` |
| `dispatch.refused_before_provider` | attempts, effects | The expected refusal code is on every attempt, none reached the provider, and the simulator ledger is empty |
| `effect.at_most_once` | effects, operations | At most one effect per reservation, and every effect belongs to an admitted operation |
| `budget.held_until_confirmed` | gate events, effects | No reservation was released without a confirmation, and none with an effect |
| `outcome.resolved_at_cutoff` | gate events, observations | At the report cutoff each reservation's state agrees with what was observed by then; an observation after the cutoff does not count |
| `evidence.offline_verdict_check` | component checks | The gate's offline verdict check accepts the published verdict |
| `evidence.verdict_request_admissible` | component checks | The request the published verdict signs passes the gate's own `requestShapeProblems`; the report also records the result with the merchant id made one scope segment |
| `evidence.verdict_permits_through_gate` | decisions | That verdict also yields a permit through `decide()` |

## The independent handoff check

`checkout_runner.handoff` re-derives `receipt_id`, the Ed25519 signature and `decision_ref` from the bytes, with the encoder and verifier from `agent-evidence-vectors` 0.18.1. It never calls the gate's code or SDK, and it takes the gate id, key id and public key from the executor's configuration, never from the record. The executor then binds the dispatched request to the receipt's `action_ref`, refuses at or after `valid_until`, writes `dispatch_pending` before the provider call, and keys the provider call to the permit, so a retry is the same operation.

## Run limits

Every report also lists, as NOT_ESTABLISHED, what no case can show: live provider behaviour, more than one gate host, a separate signed admission record from a driven component, an independent operator, and settlement or fulfilment.

## Bundle layout

```text
bundle/
  report.json, report.dsse.json, SHA256SUMS
  cases/<pin>/<case>/
    case.json            the declared expectations and the executor's trust configuration
    decisions.jsonl      gate decisions, outcomes, reconciles, final reservation states
    attempts.jsonl       the executor's attempt rows
    operations.jsonl     the executor's operation rows
    effects.jsonl        the simulator ledger
    observations.jsonl   report-time reads of the ledger, each with its time
    gate_events.jsonl    the gate's reservation transitions
    runner.jsonl         process kills and restarts
  components/<suite>/
    steps.json, NN.stdout, NN.stderr, and the suite's own result files
  reader/                an external reader's output, outside the seal
```

The report envelope is signed with a published test key. It shows the bundle was not edited after sealing and carries no authority.
