# RET-C2-252 — Unit Tests: EventTypeClassifyNode
#
# Inner DomainWorkflowGraph node 2: map the raw event-type string (+ channel) to
# a canonical event type with a rule-based confidence. Deterministic keyword
# lookup — no LLM in v1.
#
# Asserted against the MERGED develop implementation (911c1ead):
#   - Known event-type tokens / synonyms map to the correct canonical value.
#   - An unrecognised event type -> event_type="other", out_of_scope=True.
#   - out_of_scope passthrough: if already True upstream, classification is
#     skipped and the flag is preserved (no reclassification).
#
# S-4 audit: emit_trace_event is muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared package).

import pytest

from src.nodes.event_type_classify import EventTypeClassifyNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.event_type_classify.emit_trace_event",
        lambda *a, **k: None,
    )


class TestEventTypeClassifyNode:
    """Unit tests for EventTypeClassifyNode (canonical event-type mapping)."""

    def setup_method(self):
        self.node = EventTypeClassifyNode()

    def test_exact_canonical_event_type(self):
        """An exact canonical token -> that canonical type, confidence 1.0."""
        result = self.node.execute({"event_type_raw": "purchase", "channel_id": "ec", "out_of_scope": False})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["event_type"] == "purchase"
        assert result["event_type_confidence"] == 1.0
        assert result["out_of_scope"] is False

    def test_synonym_maps_to_canonical(self):
        """A synonym token (e.g. 'refund') maps to its canonical type ('return')."""
        result = self.node.execute({"event_type_raw": "refund", "channel_id": "ec", "out_of_scope": False})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["event_type"] == "return"
        assert result["out_of_scope"] is False

    def test_unknown_event_type_is_out_of_scope(self):
        """An unrecognised event type -> event_type='other', out_of_scope=True."""
        result = self.node.execute({"event_type_raw": "teleport_xyz", "channel_id": "ec", "out_of_scope": False})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["event_type"] == "other"
        assert result["out_of_scope"] is True

    def test_out_of_scope_passthrough_does_not_reclassify(self):
        """out_of_scope already True upstream -> skip classification, preserve flag."""
        result = self.node.execute({"event_type_raw": "purchase", "channel_id": "ec", "out_of_scope": True})

        assert result["status"] == AgentStatus.SUCCESS
        # Even with a recognisable raw type, an upstream out_of_scope is preserved.
        assert result["out_of_scope"] is True
        assert result["event_type"] == "other"
        assert result["event_type_confidence"] == 0.0
