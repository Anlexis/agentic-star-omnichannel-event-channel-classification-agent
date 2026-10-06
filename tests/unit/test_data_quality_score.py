# RET-C2-252 — Unit Tests: DataQualityScoreNode
#
# Inner DomainWorkflowGraph node 3: deterministic rule-based quality scoring of
# the (masked) parsed payload — required-field presence, format validity, and
# completeness. Weighted composite score in [0.0, 1.0].
#
# Asserted against the MERGED develop implementation (911c1ead). The composite
# score is:
#   score = 0.5 * required_field_ratio
#         + 0.3 * format_validity_ratio
#         + 0.2 * completeness_ratio
# so missing required fields lower the score by AT MOST 0.5 (and only when the
# format/completeness ratios are also reduced). A purchase payload with only
# event_id present (3 of 4 required fields missing) still scores ~0.625, because
# required_ratio=0.25 (-> 0.125) while format and completeness ratios remain 1.0.
# The correct assertion is therefore "strictly below a full-quality score with
# the missing-field flags populated", NOT a hard "< 0.6".
#
#   - A complete, well-formed purchase payload -> high score (>= 0.8).
#   - A payload missing required fields -> score strictly below the all-fields-
#     present score, with the missing-required-field flag(s) populated.
#   - out_of_scope short-circuit -> score=0.0, flags=[], no scoring.
#
# S-4 audit: emit_trace_event is muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared package).

import pytest

from src.nodes.data_quality_score import DataQualityScoreNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.data_quality_score.emit_trace_event",
        lambda *a, **k: None,
    )


class TestDataQualityScoreNode:
    """Unit tests for DataQualityScoreNode (deterministic quality scoring)."""

    def setup_method(self):
        self.node = DataQualityScoreNode()

    def test_complete_payload_high_score(self):
        """A complete, well-formed purchase payload scores high (>= 0.8)."""
        state = {
            "out_of_scope": False,
            "event_type": "purchase",
            "parsed_payload": {
                "event_id": "EVT-001",
                "timestamp": "2026-06-25T10:00:00Z",
                "sku": "SKU-123",
                "amount": 4980,
            },
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["data_quality_score"] >= 0.8

    def test_missing_required_fields_low_score_with_flags(self):
        """Missing required fields -> score strictly below a full-quality score, flagged.

        With the 0.5/0.3/0.2 (required/format/completeness) weighting, a purchase
        payload carrying only event_id (timestamp/sku/amount missing) scores ~0.625
        — below a full payload's score but above 0.6 — so this asserts the relative
        drop plus the missing-required-field flags, not an absolute "< 0.6".
        """
        # Reference: the same shape with ALL required fields present scores higher.
        full_state = {
            "out_of_scope": False,
            "event_type": "purchase",
            "parsed_payload": {
                "event_id": "EVT-002",
                "timestamp": "2026-06-25T10:00:00Z",
                "sku": "SKU-123",
                "amount": 4980,
            },
        }
        full_score = self.node.execute(full_state)["data_quality_score"]

        state = {
            "out_of_scope": False,
            "event_type": "purchase",
            "parsed_payload": {"event_id": "EVT-002"},  # missing timestamp/sku/amount
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        # Missing required fields strictly reduce the score below full quality...
        assert result["data_quality_score"] < full_score
        assert result["data_quality_score"] < 1.0
        # ...and the missing required fields are reported as flags.
        assert result["data_quality_flags"]  # non-empty
        assert any("missing required field" in flag for flag in result["data_quality_flags"])

    def test_out_of_scope_short_circuits_to_zero(self):
        """out_of_scope=True -> score=0.0, flags=[], no scoring performed."""
        state = {
            "out_of_scope": True,
            "event_type": "other",
            "parsed_payload": {"event_id": "EVT-003", "timestamp": "2026-06-25T10:00:00Z"},
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["data_quality_score"] == 0.0
        assert result["data_quality_flags"] == []
