"""A declared configuration value must change what the agent does.

This is the test the migration to `config/config.yaml` exists for. Three
separate things had to be true at once for a declared threshold to reach the
rule that uses it, and none of them announces itself when it is missing:

  - the entry point must construct the graph WITH the config, not bare;
  - the main slot must carry it into the inner graph;
  - each node must resolve it somewhere the framework actually calls.

That last one is the quiet one. The framework calls a node as `node(state)` —
one argument. A node written to read its thresholds from an `execute(state,
config=None)` parameter reads `None` on every real invocation and falls back to
its defaults for the life of the deployment, while a unit test that passes
`config=` by hand proves the fallback works.
"""

import json

import pytest
from fastapi.testclient import TestClient
from framework.errors import ConfigError
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.api.server import app
from src.graph.graph import Graph
from src.services.runtime_config import runtime_config
from tests.integration.test_invoke_contract import AUTH_TOKEN, BASE_EVENT

CTX = InvocationContext(session_id="test", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)


def invoke_with(config, event):
    agent = Graph(config=config)
    agent.compile()
    return agent.invoke(json.dumps(event), ctx=CTX).get("output") or {}


# ── The declared value reaches the rule ──────────────────────────────────────


def test_the_routing_threshold_changes_the_routing_decision():
    event = {k: v for k, v in BASE_EVENT.items() if k != "sku"}

    permissive = invoke_with({"domain": {"quality_threshold": 0.1}}, event)
    strict = invoke_with({"domain": {"quality_threshold": 0.99}}, event)

    assert (
        permissive["data_quality_score"] == strict["data_quality_score"]
    ), "the two runs should differ only in the threshold, not in the score"
    assert permissive["routing_decision"] == "realtime"
    assert strict["routing_decision"] == "manual_review"


def test_the_price_discrepancy_ceiling_changes_the_compliance_finding():
    event = {**BASE_EVENT, "price": 14000, "reference_price": 10000}

    strict = invoke_with({"domain": {"price_discrepancy_ratio": 0.20}}, event)
    relaxed = invoke_with({"domain": {"price_discrepancy_ratio": 0.90}}, event)

    assert strict["keihin_compliant"] is False
    assert relaxed["keihin_compliant"] is True


def test_the_realtime_event_set_changes_which_events_flow_through():
    batch_only = invoke_with({"domain": {"realtime_event_types": ["complaint"]}}, BASE_EVENT)
    included = invoke_with({"domain": {"realtime_event_types": ["purchase"]}}, BASE_EVENT)

    assert batch_only["routing_decision"] == "batch"
    assert included["routing_decision"] == "realtime"


def test_a_presumed_consent_channel_changes_the_consent_status():
    event = {k: v for k, v in BASE_EVENT.items() if k != "consent"}

    default = invoke_with({}, event)
    presumed = invoke_with({"domain": {"presumed_consent_channels": ["ec"]}}, event)

    assert default["appi_consent_status"] == "unknown"
    assert presumed["appi_consent_status"] == "granted"


def test_a_cross_channel_consent_mapping_suppresses_the_flag():
    event = {**BASE_EVENT, "consent_source_channel": "store"}

    unmapped = invoke_with({}, event)
    mapped = invoke_with({"domain": {"cross_channel_consent": ["store:ec"]}}, event)

    assert any("cross-channel" in flag for flag in unmapped["appi_flags"])
    assert not any("cross-channel" in flag for flag in mapped["appi_flags"])


# ── The shipped file is the one that is live ─────────────────────────────────


def test_the_shipped_config_declares_the_domain_block():
    config = runtime_config()
    assert config.get("max_retry"), "config/config.yaml declares no max_retry"
    assert config.get("timeout_s"), "config/config.yaml declares no timeout_s"
    assert isinstance(config.get("domain"), dict), "config/config.yaml declares no domain block"


def test_the_entry_point_runs_with_the_shipped_config(monkeypatch):
    """The standalone server must build the graph WITH the file, not bare.

    Read off the object the module actually constructed, so a future edit that
    drops the argument fails here rather than silently reverting every declared
    value to a default.
    """
    from src.api import server

    assert server.agent.config == runtime_config(), (
        "src/api/server.py is not running with config/config.yaml — a bare Graph() "
        "leaves every declared value replaced by a node default"
    )

    monkeypatch.setenv("INVOKE_AUTH_TOKEN", AUTH_TOKEN)
    body = (
        TestClient(app)
        .post(
            "/invoke",
            json={"input": json.dumps(BASE_EVENT)},
            headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
        )
        .json()
    )
    assert body["status"] == "success"


# ── A bad declaration fails to compile ───────────────────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        {"quality_threshold": float("nan")},
        {"quality_threshold": float("inf")},
        {"quality_threshold": 5.0},
        {"quality_threshold": "not a number"},
        {"quality_threshold": True},
        {"price_discrepancy_ratio": float("nan")},
        {"realtime_event_types": "purchase"},
        {"realtime_event_types": [1, 2]},
    ],
)
def test_a_bad_declared_value_refuses_to_compile(bad):
    """Refusing to start beats silently reverting to a default nobody chose.

    A NaN threshold is the case that motivates this: it parses, it is a float,
    and it compares False against every score — so the agent would start
    cleanly and answer every request as though the rule did not exist.
    """
    agent = Graph(config={"domain": bad})
    with pytest.raises(ConfigError):
        agent.compile()
