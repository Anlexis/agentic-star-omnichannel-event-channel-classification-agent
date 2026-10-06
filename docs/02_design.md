# Template Design Specification — RET-C2-252 OmnichannelDataIntegrationAgent

Classifies one retail customer event, checks it against two Japanese regulatory rules, and
decides how it should be routed onward. Deterministic throughout: no model is called, so the
same event always produces the same decision.

## Position in the architecture

| | |
|---|---|
| Agent class | `OmnichannelClassificationAgent` (`src/graph/graph.py`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Category | Cat 2 — a domain pipeline behind the standard backbone |
| Composition | Two-layer. The backbone's `main` slot holds a `GraphNode` that delegates to an inner `BaseGraph` (`src/graph/domain_workflow_graph.py`) |
| Error propagation | `propagate` — an inner failure is re-raised rather than converted into a partial success the caller cannot distinguish from a real one |
| Generation mode | Deterministic. No model is invoked anywhere in `src/` |

Three-layer separation:

- **State** — a flat `TypedDict` (`src/schemas/state.py`). Checkpoints are serialised with msgpack,
  so Pydantic models and dataclasses are not usable here.
- **Node** — each domain step is a `FunctionNode` subclass overriding `execute()` only.
- **Graph** — composition through `register_nodes()`; the backbone's edges are the framework's and
  are not overridden.

## Data flow

```
START → initialize → pre_process → main → post_process → finalize → END
                                    │
                                    └── inner graph:
                                        event_parse_channel_identify
                                          → event_type_classify
                                          → data_quality_score
                                          → keihin_compliance_check
                                          → appi_consent_check
                                          → routing_decide
                                          → output_validate
```

The inner pipeline is linear. There is no branch, which is the point: no path can reach the end
without passing both compliance checks and the output boundary. A step that has nothing to do —
an out-of-scope event reaching the pricing rule — short-circuits inside its own node and still
runs.

### Backbone nodes

| Node | Responsibility | Reads | Writes |
|---|---|---|---|
| `initialize` | Framework default — session, trust level, trace id | — | `trace_id`, `caller_trust_level` |
| `pre_process` | Input boundary over the request TEXT: empty, oversized, control markers | `user_input` | `validated_input`, `status`, `error_log` |
| `main` | Delegates to the inner graph, carrying the declared configuration into it | `validated_input` | `classification_result`, `result`, `status`, `out_of_scope`, `error_log` |
| `post_process` | Output boundary — releases the result or withholds it and clears the fields it travelled in | `result` | `formatted_output`, `result`, `classification_result`, `status` |
| `finalize` | Framework default — response metadata, timings | — | `response_metadata` |

### Inner pipeline nodes

| Node | Responsibility | Reads | Writes |
|---|---|---|---|
| `event_parse_channel_identify` | Decode the payload; screen the DECODED object; remove personal data; identify the channel | `user_input` | `parsed_payload`, `event_type_raw`, `channel_id`, `channel_confidence`, `out_of_scope` |
| `event_type_classify` | Canonicalise the event type against a closed vocabulary | `event_type_raw`, `out_of_scope` | `event_type`, `event_type_confidence`, `out_of_scope` |
| `data_quality_score` | Required-field presence, format validity, completeness | `parsed_payload`, `event_type` | `data_quality_score`, `data_quality_flags` |
| `keihin_compliance_check` | 景品表示法 — cross-channel pricing, premium rate, loyalty-point rate | `parsed_payload`, `event_type` | `keihin_compliant`, `keihin_violations` |
| `appi_consent_check` | APPI — per-channel consent status and cross-channel scope | `parsed_payload`, `channel_id` | `appi_consent_status`, `appi_flags` |
| `routing_decide` | Priority-ordered rule engine over every upstream signal | all of the above | `routing_decision`, `routing_priority` |
| `output_validate` | Assemble the result and enforce the output boundary | all of the above | `classification_result`, `result`, `status` |

## State

`src/schemas/state.py` extends the framework's `AgentState`. Every field is a primitive or a
JSON-serialisable container. Raw personal data is never written: the parser masks it before the
first state key exists, so `parsed_payload` is the only representation of the event body that any
later node can read.

Credentials are never written to state at all, and none are required — the manifest declares
`requires.secrets: []`, derived from the code rather than assumed, because nothing in `src/` calls
`ctx.secrets.require()`.

## Configuration

Two files, with different lifetimes:

- `config/agent.yaml` — the manifest. Identity, entry point, declared trust level, and the
  compile-time `requires` gates. Read by the registry at load.
- `config/config.yaml` — runtime parameters. Passed to the graph as `Graph(config=...)` by the
  registry, and loaded by `src/services/runtime_config.py` for the standalone entry point so a
  declared value is live in both deployments.

Every threshold is resolved in a node's constructor, not inside `execute()`. The framework calls a
node as `node(state)` — one argument — so a threshold read from an `execute(state, config=None)`
parameter would read `None` on every real invocation and silently use its default for the life of
the deployment.

Values are read through a bounded accessor. A declared number that is non-numeric, non-finite, or
outside its range stops the agent from compiling. A `NaN` threshold is the case that motivates
this: it parses, it is a float, and it compares `False` against every score — so the agent would
start cleanly and answer every request as though the rule did not exist.

## Boundaries

### Input

Two screens, in different places, because neither subsumes the other.

`pre_process` sees the request as TEXT. `event_parse_channel_identify` sees it DECODED. A
chat-template control marker written literally is visible in the text and is an ordinary string
value by the time it is decoded; one written with JSON escapes is invisible in the text and plain
once decoded. The decoded screen walks depth-first and reads mapping KEYS as well as values.

The marker set is treated as a class — `<|…|>`, `[INST]`, `<<SYS>>`, `<s>`, `<system>` — rather
than as a list of directive phrases. `<<SYS>>` is in it deliberately: the framework's own injection
scoring returns nothing for that one while scoring the others as high confidence, so it is the
member most likely to arrive unscreened.

Refusal is enforced in the template, not left to the platform's input gate. That gate is a property
of the deployment; where it is absent or configured off, a payload the template did not refuse
reaches the answer path and returns a success.

Every caller-controlled number goes through a finite, bounded parser. Booleans are rejected rather
than coerced — `float(True)` is `1.0`, so an unguarded parser reads `true` as one yen.

Structural limits: request body, payload field count, nesting depth, and the length of each token
compared against a vocabulary.

### Output

Four invariants, enforced in `src/nodes/output_validate.py` and independently at the backbone's
output stage:

1. no personal data — no address and no telephone number survives into the response;
2. no credential-shaped value;
3. a routing decision from the closed set, with every declared field present;
4. no raw monetary line item — the response carries ratios and the identity of the rule that was
   breached, never the amounts the caller sent.

The fourth is this agent's form of the output-precision question. It classifies one caller-supplied
event rather than aggregating many, so the question is not what precision an aggregate is rounded
to; it is whether the amounts come back out at all, and they do not.

The boundary SCANS. It does not rewrite: a boundary that quietly repairs its own output cannot tell
anyone that the layer in front of it failed. Masking is the input boundary's job and it has already
run.

Credential detection takes the UNION of the framework's detector and a local rule. The framework's
patterns describe credential FORMATS; the local rule describes a credential HABIT — a secret written
next to its name. Neither is a superset of the other, and narrowing to either one alone is a bypass
rather than a simplification.

On a refusal every output-bearing field is written back present and empty. Node results are merged
into state, so a field merely left out of the refusal keeps the value it already had — and the
framework's projection returns `formatted_output or result` even on an error status, which is
exactly the field that would still be holding the refused content. The replacement notice is
non-empty for the same reason: a falsy `formatted_output` re-opens the fallback it exists to close.

### Trust

Every node declares `required_trust_level` explicitly. The framework refuses to define a subclass
that leaves it implicit, so a missing declaration is an import-time failure that takes the whole
entry point down with it.

Every node declares the level the manifest declares. A node above that value denies the callers the
manifest invites — not at the boundary where it would be obvious, but partway through the pipeline.
`tests/integration/test_manifest_identity_alignment.py` reads both the manifest and the compiled
graphs and holds them together.

## Framework facilities used

- `InvocationContext` — session, correlation and caller identity.
- `TrustLevel` — the S-1 gate's source of truth.
- `detect_credentials_in_value` — the floor of the output boundary's credential check.
- `emit_trace_event` — one domain event per node, on a reachable path. The lifecycle events
  (`node_start`, `node_complete`, `node_error`) are emitted by the framework and must not be
  duplicated.
- The domain-specific gate hooks, never the final gate methods themselves: overriding
  `_security_gate_input` or `_security_gate_output` raises at class definition.
- `ConfigError` — raised for a declared configuration value outside its range.

## Import isolation

`src/` imports `framework.*`, `shared.*` and `langgraph` only. There is no import of the
platform SDK and no import of the retired intermediate agent tier.

## Design decisions

| Decision | Alternative considered | Chosen | Rationale |
|---|---|---|---|
| Base class | `AutonomousBaseGraph` | `AgentBaseGraph` | The pipeline is fixed and deterministic; there is no reasoning loop to run and no cost ceiling to track |
| Inner topology | Conditional edges per outcome | Linear with per-node short-circuit | No path can then skip the compliance checks or the output boundary. A conditional route is also read by the graph library as its own input schema, which projects away fields the callable is not annotated for |
| Caller channel | A second structured-parameter channel alongside `input` | `input` only | The event is one object and one channel carries it. The structured channel is returned verbatim by the backbone's first node into its own result, where the output gate scans it — so a credential-shaped value there fails the first node before any template code runs. Adding the channel would add that exposure for no domain gain |
| Amounts in the response | Echo the figures that breached a rule | Ratio and rule identity only | The caller sent the amounts; repeating them adds nothing and makes the response a channel for whatever was in those fields |
| Thresholds | Node constants | `config/config.yaml`, resolved in constructors | An operator can change policy without changing code, and a malformed value stops the agent rather than reverting silently |

## Known platform interactions

The platform's personal-data filter runs before any template code. Two of its patterns overlap
retail data:

- A twelve-digit Japanese identity-number shape matches an event reference that embeds a date —
  `evt-<yyyymmdd>-<nnnn>` is eight digits plus four — so the field arrives already rewritten.
- A title-case name shape matches store and product names.

Neither is fixable from here. What the template controls is its response: a rewritten field is
never certified as valid data, and the quality flag says the value was redacted rather than
blaming the caller's formatting. `tests/integration/test_invoke_contract.py` pins the behaviour so
that a narrowing of those patterns is noticed rather than assumed.
