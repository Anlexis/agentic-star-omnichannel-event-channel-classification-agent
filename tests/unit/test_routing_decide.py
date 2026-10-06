# RET-C2-252 — Unit Tests: RoutingDecideNode
#
# Inner DomainWorkflowGraph node 6: final routing decision from all upstream
# classification + compliance signals. Deterministic, priority-ordered rule
# engine; thresholds / routing sets configurable via node_config. No LLM in v1.
#
# Priority order (first match wins): out_of_scope -> ERROR -> keihin -> appi
#   -> low_quality -> realtime_event -> default_batch.
#
# Asserted against the MERGED develop implementation (911c1ead):
#   - keihin_compliant=False -> manual_review, priority 1.
#   - appi_consent_status="denied" -> manual_review, priority 1.
#   - data_quality_score below threshold -> manual_review, priority 2.
#   - out_of_scope=True -> manual_review, priority 2.
#   - A purchase event with OK quality and clean compliance -> realtime, priority 1.
#   - A low-priority (browse) event with OK quality -> batch, priority 3.
#
# S-4 audit: emit_trace_event is muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared package).

import pytest

from src.nodes.routing_decide import RoutingDecideNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.routing_decide.emit_trace_event",
        lambda *a, **k: None,
    )


def _base_state(**overrides):
    """A clean, compliant, high-quality purchase signal set."""
    base = {
        "out_of_scope": False,
        "status": AgentStatus.SUCCESS,
        "channel_id": "ec",
        "channel_confidence": 1.0,
        "event_type": "purchase",
        "event_type_confidence": 1.0,
        "data_quality_score": 0.95,
        "data_quality_flags": [],
        "keihin_compliant": True,
        "appi_consent_status": "granted",
    }
    base.update(overrides)
    return base


class TestRoutingDecideNode:
    """Unit tests for RoutingDecideNode (priority-ordered routing rule engine)."""

    def setup_method(self):
        self.node = RoutingDecideNode()

    def test_keihin_non_compliant_manual_review_priority_1(self):
        """keihin_compliant=False -> manual_review, priority 1."""
        result = self.node.execute(_base_state(keihin_compliant=False))

        assert result["status"] == AgentStatus.SUCCESS
        assert result["routing_decision"] == "manual_review"
        assert result["routing_priority"] == 1

    def test_appi_denied_manual_review_priority_1(self):
        """appi_consent_status='denied' -> manual_review, priority 1."""
        result = self.node.execute(_base_state(appi_consent_status="denied"))

        assert result["status"] == AgentStatus.SUCCESS
        assert result["routing_decision"] == "manual_review"
        assert result["routing_priority"] == 1

    def test_low_quality_manual_review_priority_2(self):
        """data_quality_score below threshold -> manual_review, priority 2."""
        result = self.node.execute(_base_state(data_quality_score=0.3))

        assert result["status"] == AgentStatus.SUCCESS
        assert result["routing_decision"] == "manual_review"
        assert result["routing_priority"] == 2

    def test_out_of_scope_manual_review_priority_2(self):
        """out_of_scope=True -> manual_review, priority 2."""
        result = self.node.execute(_base_state(out_of_scope=True))

        assert result["status"] == AgentStatus.SUCCESS
        assert result["routing_decision"] == "manual_review"
        assert result["routing_priority"] == 2

    def test_purchase_quality_ok_realtime_priority_1(self):
        """A purchase event with OK quality and clean compliance -> realtime, priority 1."""
        result = self.node.execute(_base_state())

        assert result["status"] == AgentStatus.SUCCESS
        assert result["routing_decision"] == "realtime"
        assert result["routing_priority"] == 1

    def test_low_priority_event_quality_ok_batch_priority_3(self):
        """A browse event (not in realtime set) with OK quality -> batch, priority 3."""
        result = self.node.execute(_base_state(event_type="browse"))

        assert result["status"] == AgentStatus.SUCCESS
        assert result["routing_decision"] == "batch"
        assert result["routing_priority"] == 3
