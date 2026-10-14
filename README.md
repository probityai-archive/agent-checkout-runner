# agent-checkout-runner

A combined runner for agent checkout and refund components: it drives each component's own code, keeps admission decisions, dispatch attempts, simulator effects and report-time observations in separate streams, and reports every claim as ESTABLISHED, CONTRADICTED or NOT_ESTABLISHED.

It is for component owners and reviewers who want one reproducible run, with negative and recovery cases, that a separate reader can re-check from the retained files.

## Install

```sh
uv tool install git+https://github.com/probityai/agent-checkout-runner@v0.1.0
```

It pins `agent-evidence-vectors==0.18.1` for JCS and Ed25519. It needs git, Node 24 and npm.

## Run

```sh
agent-checkout-runner run --out bundle
agent-checkout-runner verify bundle
```

The first command fetches every pinned component, runs the matrix and the component suites, and seals the bundle. The second re-checks the sums and the signed report, then re-derives every claim from the retained streams. A run at v0.1.0 prints, in part:

```text
ok   main lost_response_retry          decisions_verify=E ... at_most_once=E held_until_confirmed=E resolved_at_cutoff=E
ok   main lost_response_after_cutoff   decisions_verify=E ... resolved_at_cutoff=N
ok   main published_verdict            ... offline_verdict_check=E verdict_request_admissible=C verdict_permits_through_gate=C
26/26 cases match their declared claims
ESTABLISHED      suite pic-refund-approval: 2 cases steps exited 0 at eb7cd4672add
ESTABLISHED      suite aps-refund-exact-approval: 1 cases steps exited 0 at c31d94aad867
ESTABLISHED      suite refund-predicates-c1-c3: 5 cases steps exited 0 at c039b7cd7bfe
ESTABLISHED      suite refund-handoff-0.1: 1 cases steps exited 0 at 38e75b16a967
bundle verifies
```

Hand the sealed bundle to an independent reader with `--reader "<command> {bundle}"`. Its exit status and output are kept under `reader/` and decide nothing in the runner.

## Run it in your own CI

```yaml
- uses: probityai/agent-checkout-runner@v0.1.0
  with:
    gate-dir: path/to/your/aps-checkout-gate   # empty: the pinned commit
```

With `gate-dir`, the matrix runs against your checkout and the report records its commit as a host checkout.

## What a run is and is not

Every record is synthetic, the provider is a local simulator and the keys are public test keys. A run by this repository's author is a fixture run: it is not an independent run, a host adoption, a connected PIC, APS and Conduit result, or a merchant deployment. The component suites run side by side on their own fixtures and say nothing about the components being connected.

## Status

v0.1.0. The matrix drives `aps-checkout-gate` at `dedc818`. CI runs the unit tests, a full sealed run through the Action, and a host-checkout run.

## Docs

| Page | What it covers |
| --- | --- |
| [Coverage map](https://github.com/probityai/agent-checkout-runner/blob/main/docs/COVERAGE-MAP.md) | What each case and suite covers, who owns each component, and what a connected run still needs |
| [Claims](https://github.com/probityai/agent-checkout-runner/blob/main/docs/CLAIMS.md) | Each claim, the streams it reads, and the bundle layout |
