# RET-C2-252 — Unit Tests: APPIConsentCheckNode
#
# Inner DomainWorkflowGraph node 5: 改正個人情報保護法 (APPI 2026) per-channel
# consent-status check. Deterministic rule checks; channel-specific consent field
# mappings configurable via node_config. No LLM in v1.
#
# Asserted against the MERGED develop implementation (911c1ead):
#   - Explicit consent True -> appi_consent_status="granted".
#   - Explicit consent False -> appi_consent_status="denied".
#   - No consent field -> appi_consent_status="unknown" (default channel posture).
#   - Cross-channel scope: a consent sourced from channel A applied to channel B
#     with no mapping raises an appi_flag.
#   - appi_flags name the event/channel, never the customer (no PII pattern).
#   - out_of_scope short-circuit -> "unknown", flags=[].
#
# S-4 audit: emit_trace_event is muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared package).

import pytest

from framework.schemas.agent_status import AgentStatus
from src.nodes.appi_consent_check import APPIConsentCheckNode

# The personal-data rules are imported, never restated. A private copy here
# would keep passing after the real one changed, which is the one thing this
# assertion exists to prevent.
from src.nodes.redaction import EMAIL_RE as _EMAIL_RE
from src.nodes.redaction import find_phone as _find_phone


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.appi_consent_check.emit_trace_event",
        lambda *a, **k: None,
    )


class TestAPPIConsentCheckNode:
    """Unit tests for APPIConsentCheckNode (per-channel consent)."""

    def setup_method(self):
        self.node = APPIConsentCheckNode()

    def test_explicit_consent_true_granted(self):
        """Explicit consent True -> appi_consent_status='granted'."""
        state = {
            "out_of_scope": False,
            "channel_id": "ec",
            "event_type": "purchase",
            "parsed_payload": {"event_id": "EVT-020", "consent": True},
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["appi_consent_status"] == "granted"

    def test_explicit_consent_false_denied(self):
        """Explicit consent False -> appi_consent_status='denied'."""
        state = {
            "out_of_scope": False,
            "channel_id": "ec",
            "event_type": "purchase",
            "parsed_payload": {"event_id": "EVT-021", "consent": False},
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["appi_consent_status"] == "denied"

    def test_no_consent_field_unknown(self):
        """No consent field -> appi_consent_status='unknown' (default posture)."""
        state = {
            "out_of_scope": False,
            "channel_id": "ec",
            "event_type": "purchase",
            "parsed_payload": {"event_id": "EVT-022"},
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["appi_consent_status"] == "unknown"
        assert result["appi_flags"]  # flagged: no explicit signal

    def test_cross_channel_consent_raises_flag(self):
        """Consent sourced from another channel with no mapping -> appi_flag raised."""
        state = {
            "out_of_scope": False,
            "channel_id": "store",
            "event_type": "purchase",
            "parsed_payload": {
                "event_id": "EVT-023",
                "consent": True,
                "consent_source_channel": "ec",  # different from channel_id, no mapping
            },
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        # Consent on channel A does not auto-grant for channel B without a mapping.
        assert any("cross-channel" in f for f in result["appi_flags"])

    def test_appi_flags_contain_no_customer_pii(self):
        """appi_flags name the event/channel, never the customer (no PII pattern)."""
        state = {
            "out_of_scope": False,
            "channel_id": "ec",
            "event_type": "purchase",
            "parsed_payload": {
                "event_id": "EVT-024",
                "email": "taro.yamada@example.com",
                "phone": "090-1234-5678",
            },
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        flags_text = " ".join(result["appi_flags"])
        assert not _EMAIL_RE.search(flags_text)
        assert _find_phone(flags_text) is None
        assert "taro.yamada@example.com" not in flags_text

    def test_out_of_scope_short_circuits_unknown(self):
        """out_of_scope=True -> 'unknown', flags=[] (skip evaluation)."""
        state = {
            "out_of_scope": True,
            "channel_id": "ec",
            "event_type": "other",
            "parsed_payload": {"consent": True},
        }
        result = self.node.execute(state)

        assert result["status"] == AgentStatus.SUCCESS
        assert result["appi_consent_status"] == "unknown"
        assert result["appi_flags"] == []
