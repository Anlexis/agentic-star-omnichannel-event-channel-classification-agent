"""The backbone runs in the declared order, for a real external caller.

This is the boundary proof that the whole agent — not a node, not the inner
graph, but the compiled thing a caller reaches — executes its five stages in
order and returns a real answer at the end.

It matters that the caller here is constructed as an ordinary external one,
with the trust level the manifest tells callers to present. A proof driven with
an internal context proves the pipeline works for a caller who will never exist.
"""

import json

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.utils.config_loader import load_config
from pathlib import Path

from src.graph.graph import Graph
from src.services.runtime_config import runtime_config
from tests.integration.test_invoke_contract import BASE_EVENT

_MANIFEST = load_config(str(Path(__file__).resolve().parents[2] / "config" / "agent.yaml"))

EXPECTED_ORDER = [
    "InitializeNode",
    "PreProcessNode",
    "DomainWorkflowGraphNode",
    "PostProcessNode",
    "FinalizeNode",
]


@pytest.fixture()
def agent():
    graph = Graph(config=runtime_config())
    graph.compile()
    return graph


@pytest.fixture()
def caller_ctx():
    """An external caller presenting exactly what the manifest asks for."""
    return InvocationContext(
        session_id="pb-invoke-order",
        caller_trust_level=TrustLevel(_MANIFEST["required_trust_level"]),
    )


def test_the_backbone_runs_every_stage_in_order(agent, caller_ctx):
    result = agent.invoke(json.dumps(BASE_EVENT), ctx=caller_ctx)

    assert result["status"] == "success"
    assert result["node_history"] == EXPECTED_ORDER


def test_the_invocation_returns_a_real_classification(agent, caller_ctx):
    """A run that completes in order but returns nothing has proved very little."""
    output = agent.invoke(json.dumps(BASE_EVENT), ctx=caller_ctx)["output"]

    assert output["channel_id"] == "ec"
    assert output["event_type"] == "purchase"
    assert output["routing_decision"] in {"realtime", "batch", "manual_review"}
    assert output["out_of_scope"] is False


def test_the_declared_caller_trust_level_is_sufficient(agent, caller_ctx):
    """The manifest's own trust level must carry a request all the way through.

    Every node declares a minimum, and a node above the manifest's value denies
    the caller partway down the pipeline — which surfaces as an error status on
    a request that looked correctly authenticated.
    """
    result = agent.invoke(json.dumps(BASE_EVENT), ctx=caller_ctx)

    assert result["status"] == "success", (
        "a caller presenting the declared trust level was denied: " f"{result.get('error_log')}"
    )
    assert result["node_history"] == EXPECTED_ORDER


def test_a_caller_below_the_declared_trust_level_is_denied(agent):
    """The other direction: the trust gate is not simply open."""
    anonymous = InvocationContext(session_id="pb-invoke-order-anon", caller_trust_level=TrustLevel.ANONYMOUS)
    result = agent.invoke(json.dumps(BASE_EVENT), ctx=anonymous)

    assert result["status"] == "error"
    assert result["output"] is None


def test_the_deployment_payload_is_the_one_this_proof_uses():
    """The smoke payload and this proof must assert the same contract.

    The deployment posts `deploy/invoke_payload.json` verbatim. If it drifts
    from the fixture used here, this proof stops being evidence about what is
    actually deployed.
    """
    payload = json.loads((Path(__file__).resolve().parents[2] / "deploy" / "invoke_payload.json").read_text())
    assert json.loads(payload["input"]) == BASE_EVENT
