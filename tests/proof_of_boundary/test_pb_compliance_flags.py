# RET-C2-252 — Proof-of-Boundary: compliance + out-of-scope boundary scenarios
#
# These exercise the real domain-node chain end-to-end (no mocks beyond the S-4
# audit mute) to prove the regulatory boundaries hold:
#
#   PoB-1  景品表示法 (cross-channel pricing): a SKU whose channel price deviates
#          from its cross-channel reference beyond the threshold -> a purchase
#          flows through to keihin_compliant=False and routing=manual_review.
#   PoB-2  APPI (denied consent): an event carrying explicit consent=False ->
#          appi_consent_status="denied" and routing=manual_review.
#   PoB-3  Out-of-scope (no channel field): the input boundary marks
#          out_of_scope=True, status stays SUCCESS (NOT ERROR), and routing is
#          manual_review.
#
# Asserted against the MERGED develop implementation (911c1ead). The inner graph
# topology is linear, so the chain is composed by hand here:
#   event_parse_channel_identify -> event_type_classify -> data_quality_score
#     -> keihin_compliance_check -> appi_consent_check -> routing_decide
#     -> output_validate
#
# S-4 audit: emit_trace_event is muted at EVERY node module under test via an
# autouse fixture (NEVER stub shared.* in sys.modules — the CI wheel ships a real
# shared package).

import json

import pytest

from src.nodes.event_parse_channel_identify import EventParseChannelIdentifyNode
from src.nodes.event_type_classify import EventTypeClassifyNode
from src.nodes.data_quality_score import DataQualityScoreNode
from src.nodes.keihin_compliance_check import KeihinComplianceCheckNode
from src.nodes.appi_consent_check import APPIConsentCheckNode
from src.nodes.routing_decide import RoutingDecideNode
from src.nodes.output_validate import OutputValidateNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at EVERY node module under test."""
    for mod in (
        "event_parse_channel_identify",
        "event_type_classify",
        "data_quality_score",
        "keihin_compliance_check",
        "appi_consent_check",
        "routing_decide",
        "output_validate",
    ):
        monkeypatch.setattr(f"src.nodes.{mod}.emit_trace_event", lambda *a, **k: None)


# The inner domain pipeline in order — each node consumes the merged state and
# returns a partial-dict update that is folded back in.
_PIPELINE = [
    EventParseChannelIdentifyNode(),
    EventTypeClassifyNode(),
    DataQualityScoreNode(),
    KeihinComplianceCheckNode(),
    APPIConsentCheckNode(),
    RoutingDecideNode(),
    OutputValidateNode(),
]


def _run_pipeline(raw_input):
    """Drive the linear domain pipeline and return the final merged state."""
    state = {"raw_input": json.dumps(raw_input), "error_log": []}
    for node in _PIPELINE:
        state.update(node.execute(state))
    return state


class TestPoBKeihinCrossChannelPricing:
    """PoB-1: a cross-channel pricing violation routes to manual_review."""

    def test_price_discrepancy_routes_manual_review(self):
        """景表法: discrepancy beyond threshold -> non-compliant + manual_review."""
        payload = {
            "channel": "ec",
            "event_type": "purchase",
            "event_id": "EVT-100",
            "timestamp": "2026-06-25T10:00:00Z",
            "sku": "SKU-77",
            "amount": 3000,
            "price": 3000,
            "reference_price": 1000,  # 200% discrepancy >> 20% threshold
        }
        state = _run_pipeline(payload)

        assert state["status"] == AgentStatus.SUCCESS
        assert state["keihin_compliant"] is False
        assert state["routing_decision"] == "manual_review"
        # The assembled result still passes the S-3 gate (it is PII-clean).
        assert state["classification_result"]["keihin_compliant"] is False


class TestPoBAPPIDeniedConsent:
    """PoB-2: a denied-consent event routes to manual_review."""

    def test_denied_consent_routes_manual_review(self):
        """APPI: explicit consent=False -> 'denied' + manual_review."""
        payload = {
            "channel": "ec",
            "event_type": "purchase",
            "event_id": "EVT-101",
            "timestamp": "2026-06-25T10:00:00Z",
            "sku": "SKU-78",
            "amount": 4980,
            "consent": False,
        }
        state = _run_pipeline(payload)

        assert state["status"] == AgentStatus.SUCCESS
        assert state["appi_consent_status"] == "denied"
        assert state["routing_decision"] == "manual_review"


class TestPoBOutOfScopeIsSuccess:
    """PoB-3: an out-of-scope request is graceful SUCCESS, not an error boundary."""

    def test_unknown_channel_returns_success_out_of_scope(self):
        """No channel field at all -> out_of_scope=True, manual_review, SUCCESS.

        A payload with NO channel field is the genuine out-of-scope boundary in
        the merged _identify_channel (raw_channel is None -> 'other', 0.0, True).
        The flag is preserved by every downstream node; RoutingDecide rule #1
        (out_of_scope) routes to manual_review; OutputValidate treats
        out_of_scope=True as a valid SUCCESS that still passes the S-3 gate.
        """
        payload = {
            "event_type": "purchase",
            "event_id": "EVT-102",
            "timestamp": "2026-06-25T10:00:00Z",
        }
        state = _run_pipeline(payload)

        assert state["status"] == AgentStatus.SUCCESS
        assert state["out_of_scope"] is True
        assert state["routing_decision"] == "manual_review"
        assert state["classification_result"]["out_of_scope"] is True

    def test_unknown_event_type_returns_success_out_of_scope(self):
        """Unrecognised event type on a known channel -> out_of_scope=True, SUCCESS."""
        payload = {
            "channel": "ec",
            "event_type": "teleport_xyz",
            "event_id": "EVT-103",
            "timestamp": "2026-06-25T10:00:00Z",
        }
        state = _run_pipeline(payload)

        assert state["status"] == AgentStatus.SUCCESS
        assert state["out_of_scope"] is True
        assert state["routing_decision"] == "manual_review"
