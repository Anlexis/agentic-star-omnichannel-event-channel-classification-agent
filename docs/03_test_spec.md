# Test Specification — RET-C2-252 OmnichannelDataIntegrationAgent

## Strategy

193 tests across three suites. The balance is deliberate: most of the coverage sits in
`tests/integration/`, which posts to the real HTTP entry point with the real compiled graph behind
it, rather than in unit tests that hand each node a state the test author built.

That is not a stylistic preference. The defects this suite was written against were all invisible
to node-level testing — a node returns what it is handed, so a suite built from hand-made states
proves the nodes agree with the test author rather than with each other. The specific cases were: a
main slot that handed the inner graph a mapping where a string was expected, so every classification
came back as the unrecognised default; nodes demanding more caller trust than the manifest declares,
so every real request was denied partway through; and thresholds read from a parameter the framework
never supplies, so every declared value was silently a default.

Every test runs without a platform connection. The framework package is installed from the wheel
that CI installs.

| Suite | Tests | What it covers |
|---|---|---|
| `tests/integration/` | 120 | The caller contract, both input screens, the output boundary, and configuration liveness — all through the real ASGI app |
| `tests/unit/` | 55 | Per-node rules in isolation, plus the framework-compliance checks |
| `tests/proof_of_boundary/` | 18 | The mandatory boundary proofs |

## Framework compliance

| TC-ID | Test | Expected result | Where |
|---|---|---|---|
| TC-01 | State is a flat TypedDict | No Pydantic, no dataclass, primitives only | `test_state_safety.py` |
| TC-03 | No credential in State | `gate-credential-scan`: 0 violations | CI |
| TC-05 | No duplicate lifecycle events inside `execute()` | `node_start` / `node_complete` / `node_error` absent from every `execute()` body | `check_audit_trace.py` (CI) |
| TC-06 | The default input gate cannot be replaced | `TypeError` at class definition | `test_framework_compliance_tc06_tc07.py` |
| TC-07 | The default output gate cannot be replaced | `TypeError` at class definition | `test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` is enforced | A caller below the declared level is refused | `test_pb_invoke_order.py` |
| TC-09 | Every node declares its trust level explicitly | No node relies on an inherited default | `test_manifest_identity_alignment.py` |
| TC-10 | No node demands more trust than the manifest declares | A caller at the declared level completes the pipeline | `test_manifest_identity_alignment.py` |
| TC-11 | Each node emits at least one domain audit event on a reachable path | ≥1 per node | `check_audit_trace.py` (CI) |

## Proof-of-boundary

| PB-ID | Boundary | Expected result | Where |
|---|---|---|---|
| PB-2 | State serialisation | Post-invoke state is primitives only | `test_state_safety.py` |
| PB-4 | Import isolation | AST scan: no platform-SDK import, no retired-tier import | `test_import_isolation.py` |
| PB-6 | Invoke execution order | The five backbone stages run in order for an external caller, and the run returns a real classification | `test_pb_invoke_order.py` |
| PB-7 | Human-review interrupt propagation | Not enabled for this template — the main slot declares no cross-boundary propagation, so the module skips with that reason rather than asserting something trivially true | `test_pb7_hitl_interrupt_propagation.py` |
| PB — output gate | The graph-level output hook refuses an incomplete or absent result | Refused | `test_pb_s3_output_gate.py` |
| PB — compliance flags | The two regulatory findings reach the response and carry no personal data | Verified | `test_pb_compliance_flags.py` |

The deployment payload is part of this contract: `deploy/invoke_payload.json` is asserted against
the same fixture the tests use, and posted through the real app, so the body the deployment sends
and the body the tests assert cannot become two different things.

## Business logic

| BL-ID | Behaviour | Input | Expected result |
|---|---|---|---|
| BL-01 | A complete event is classified | `channel: ec`, `event_type: purchase`, all required fields | `ec` / `purchase`, quality > 0.9, routed `realtime` |
| BL-02 | Every routing outcome is reachable | five distinct events | `realtime`, `batch`, and `manual_review` each produced by a real request |
| BL-03 | The classification depends on the event | a complete event vs a sparse one | channel, event type and quality all differ |
| BL-04 | An unrecognised channel is out of scope | `channel: carrier-pigeon` | `out_of_scope`, routed `manual_review` |
| BL-05 | A cross-channel price discrepancy is flagged | `price: 14000`, `reference_price: 10000` | not compliant, one violation, routed `manual_review` |
| BL-06 | A price within tolerance is not flagged | `price: 10500`, `reference_price: 10000` | compliant, no violations |
| BL-07 | Consent absent is not consent granted | no consent field | status `unknown`, flagged |
| BL-08 | Consent denied routes to a person | `consent: denied` | routed `manual_review` |
| BL-09 | Cross-channel consent needs a mapping | `consent_source_channel: store` on an `ec` event | flagged; suppressed only when the mapping declares `store:ec` |
| BL-10 | Retail identifiers survive scoring | EAN-13 barcode, ISO-8601 timestamp | no quality flags — neither is rewritten before it is scored |

## Input boundary

| ID | Behaviour | Expected result |
|---|---|---|
| IN-01 | Chat-template control markers, six forms | Refused, no output |
| IN-02 | A control marker in a field NAME | Refused |
| IN-03 | A control marker written as JSON escapes | Refused by the decoded screen; the test names which layer catches it |
| IN-04 | A marker spliced across markup | Refused |
| IN-05 | Ordinary retail text containing `<`, `system`, `ignore` | Accepted — six real sentences |
| IN-05b | The text screen called directly, with nothing in front of it | Refuses each marker, passes ordinary text — its own behaviour is the observable, because end to end the decoded screen would refuse the same request either way |
| IN-06 | Non-finite numbers on five monetary fields | Compliance `False`, routed `manual_review` — never reported compliant |
| IN-07 | A boolean in a numeric field | Rejected, not coerced |
| IN-08 | Oversized body | HTTP 413 |
| IN-09 | Too many payload fields | Refused |
| IN-10 | A body that is not JSON | Refused |
| IN-11 | Credential shapes, five forms | HTTP 400 naming the kind, never echoing the value |
| IN-12 | An ordinary event through the same screen | Accepted |

## Output boundary

| ID | Behaviour | Expected result |
|---|---|---|
| OUT-01 | A clean result | Released — the control that keeps the rest from being vacuous |
| OUT-02 | Personal data anywhere in the result | Refused |
| OUT-03 | Personal data nested inside a flag list | Refused — the scan walks nested structures |
| OUT-04 | Credential shapes the framework detects, six forms | Not released; the credential check itself sees each one |
| OUT-05 | A credential habit the framework does not detect | Refused by the local rule; the test asserts the framework still misses it, so a redundant union is noticed |
| OUT-06 | A routing decision outside the closed set | Refused |
| OUT-07 | An incomplete or absent result | Refused |
| OUT-08 | A caller-supplied amount | Never rendered; the violation reports the ratio |
| OUT-09 | A SKU carrying a newline | Not rendered; a well-formed SKU still is |
| OUT-10 | Output-bearing fields on a refusal | Present and empty, not omitted |
| OUT-11 | The withheld notice | Non-empty — a falsy one re-opens the projection fallback |
| OUT-12 | The error envelope | No released text, no traceback, no source paths, no echo of the payload |

## Configuration

| ID | Behaviour | Expected result |
|---|---|---|
| CFG-01 | Routing quality threshold | Changes the routing decision at a fixed score |
| CFG-02 | Price-discrepancy ceiling | Changes the compliance finding for a fixed price pair |
| CFG-03 | Realtime event set | Changes which event types flow through |
| CFG-04 | Presumed-consent channels | Changes the consent status |
| CFG-05 | Cross-channel consent mapping | Suppresses the cross-channel flag |
| CFG-06 | The shipped file | Declares the domain block; the entry point runs with it |
| CFG-07 | A malformed declared value, eight forms | Refuses to compile — including `NaN`, which would otherwise parse and compare `False` against everything |

## Execution summary

- Total: 193 — 192 passed, 1 skipped (PB-7, not applicable to this template)
- Framework: the published `agenticstar-agentcore` wheel
- Warnings from repository code: 0
