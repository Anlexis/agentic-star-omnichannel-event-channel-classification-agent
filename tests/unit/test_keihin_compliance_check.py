# RET-C2-252 — Unit Tests: KeihinComplianceCheckNode
#
# Inner DomainWorkflowGraph node 4: 景品表示法 (Act against Unjustifiable Premiums
# and Misleading Representations) cross-channel pricing-consistency check.
# Deterministic rule/threshold evaluation; thresholds configurable via
# node_config. No LLM in v1.
#
# Asserted against the MERGED develop implementation (911c1ead):
#   - A non-pricing event (e.g. inquiry) -> keihin_compliant=True, violations=[].
#   - A cross-channel price discrepancy above threshold -> keihin_compliant=False,
#     violations populated.
#   - Violation strings carry product/price data only — never customer PII
#     (no email/phone/name pattern present).
#   - out_of_scope short-circuit -> compliant=True, violations=[].
#
# S-4 audit: emit_trace_event is muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared package).

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.keihin_compliance_check import KeihinComplianceCheckNode

# The personal-data rules are imported, never restated. A private copy here
# would keep passing after the real one changed, which is the one thing this
# assertion exists to prevent.
from src.nodes.redaction import EMAIL_RE as _EMAIL_RE
from src.nodes.redaction import find_phone as _find_phone


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.keihin_compliance_check.emit_trace_event",
        lambda *a, **k: None,
    )


class TestKeihinComplianceCheckNode:
    """Unit tests for KeihinComplianceCheckNode (景品表示法 cross-channel pricing)."""

    def setup_method(self):
        self.node = KeihinComplianceCheckNode()

    def test_non_pricing_event_is_compliant(self):
        """A non-pricing event type (inquiry) -> compliant=True, no violations."""
        state = {
            "out_of_scope": False,
            "event_type": "inquiry",
            "channel_id": "call_center",
            "parsed_payload": {"event_id": "EVT-010"},
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["keihin_compliant"] is True
        assert result["keihin_violations"] == []

    def test_cross_channel_price_discrepancy_flagged(self):
        """A SKU price far above its cross-channel reference -> non-compliant."""
        state = {
            "out_of_scope": False,
            "event_type": "purchase",
            "channel_id": "ec",
            "parsed_payload": {
                "event_id": "EVT-011",
                "sku": "SKU-9",
                "price": 2000,
                "reference_price": 1000,  # 100% discrepancy >> 20% threshold
            },
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["keihin_compliant"] is False
        assert result["keihin_violations"]  # non-empty

    def test_violations_contain_no_customer_pii(self):
        """Violation strings carry SKU/price data only — never customer PII."""
        state = {
            "out_of_scope": False,
            "event_type": "purchase",
            "channel_id": "ec",
            "parsed_payload": {
                "event_id": "EVT-012",
                "sku": "SKU-42",
                "price": 5000,
                "reference_price": 1000,
                # Even if PII slips into the payload, violations must not echo it.
                "email": "taro.yamada@example.com",
                "phone": "090-1234-5678",
            },
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["keihin_compliant"] is False
        violations_text = " ".join(result["keihin_violations"])
        assert not _EMAIL_RE.search(violations_text)
        assert _find_phone(violations_text) is None
        assert "taro.yamada@example.com" not in violations_text

    def test_out_of_scope_short_circuits_compliant(self):
        """out_of_scope=True -> compliant=True, violations=[] (skip evaluation)."""
        state = {
            "out_of_scope": True,
            "event_type": "purchase",
            "channel_id": "ec",
            "parsed_payload": {
                "sku": "SKU-1",
                "price": 2000,
                "reference_price": 1000,
            },
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["keihin_compliant"] is True
        assert result["keihin_violations"] == []
