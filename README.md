# Omnichannel Event Classification & Routing Agent

AI agent for classifying omnichannel customer events and routing them by channel, built with Agentic Star.

> **Category**: Cat 2 (Domain-specific workflow pipeline)
> **Industry**: Retail
> **Template ID**: RET-C2-252

## Overview

Classifies a single retail customer event — a purchase, a browse, a return, a complaint — that
arrived on any one of several channels, and decides how it should be routed onward.

A retailer running EC, physical stores, a mobile app, social channels and a loyalty programme
receives the same customer's activity in several different shapes. This agent takes one such
event, works out which channel it came from and what kind of event it is, scores how complete
and well-formed the data is, checks it against two Japanese regulatory rules, and returns a
routing decision: straight through in realtime, batched for later, or held for a person to look
at.

The two regulatory checks are the part that is specific to Japan:

- **景品表示法** (Act against Unjustifiable Premiums and Misleading Representations) — cross-channel
  pricing consistency. A SKU priced differently on the web than in store beyond a declared
  tolerance, a premium worth more than a declared fraction of the transaction it is attached to, or
  a loyalty-point scheme converting to value above a declared rate.
- **APPI** (Act on the Protection of Personal Information) — per-channel consent. Consent collected
  on one channel does not carry to another unless a mapping says it may.

Every rule is deterministic and every threshold is declared in `config/config.yaml`. No language
model is called: the same event always produces the same classification, which is what makes the
routing decision auditable.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent resolves its secrets and its caller identity through the platform. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR, and there is no degraded mode. The test suite
runs without a platform connection because it drives the graph and the HTTP adapter directly;
serving real traffic does not, because the secrets provider and the caller trust level both come
from the platform. Started without it, the agent falls back to an anonymous caller identity and
its trust gate refuses every request — deliberately, rather than answering with an identity it
could not establish.

## What the agent accepts and returns

One request carries one event, as a JSON object encoded in the `input` string:

```json
{
  "input": "{\"event_id\": \"evt-20260914T0001\", \"timestamp\": \"2026-09-14T10:15:00Z\", \"channel\": \"ec\", \"event_type\": \"purchase\", \"sku\": \"4901234567890\", \"amount\": 12500}"
}
```

The response carries the classification, the two compliance findings, and the routing decision:

```json
{
  "channel_id": "ec",
  "event_type": "purchase",
  "data_quality_score": 1.0,
  "keihin_compliant": true,
  "appi_consent_status": "unknown",
  "routing_decision": "realtime",
  "routing_priority": 1
}
```

Amounts are not echoed back. A pricing violation reports the ratio that breached the rule and the
SKU it applies to, not the figures the caller sent.

`deploy/invoke_payload.json` holds a complete working request.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent manifest and runtime configuration
deploy/       local deployment recipe and a sample request
docs/         design and test documentation
```

`docs/02_design.md` describes the pipeline and the boundaries; `docs/03_test_spec.md` maps each
test to the behaviour it covers.

## Customising

1. `config/config.yaml` holds every threshold — the routing quality bar, the two pricing ceilings,
   the consent posture per channel. A value outside its declared range stops the agent from
   starting rather than reverting to a default.
2. `src/nodes/event_parse_channel_identify.py` holds the channel vocabulary; extend the synonym
   table for the channels your business actually runs.
3. `src/nodes/event_type_classify.py` holds the event-type vocabulary.
4. `src/nodes/keihin_compliance_check.py` and `src/nodes/appi_consent_check.py` hold the regulatory
   rules. Adapt them if you operate under a different regime.
5. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
