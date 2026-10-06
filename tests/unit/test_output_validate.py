# RET-C2-252 — Unit Tests: OutputValidateNode (S-3 output gate)
#
# Inner DomainWorkflowGraph final node. Assembles the PII-clean
# classification_result and runs the S-3 output security gate via the
# module-level run_security_gate() helper (also invoked by the outer graph's
# _extra_security_gate_output, ADR-017 hook).
#
# Asserted against the MERGED develop implementation (911c1ead):
#   - A valid upstream state -> classification_result assembled, status=SUCCESS.
#   - run_security_gate() on a dict missing required keys -> (False, reason).
#   - run_security_gate() on a result with PII (email/phone) -> (False, reason).
#   - run_security_gate() on a disallowed routing_decision -> (False, reason).
#   - out_of_scope=True -> a valid output carrying out_of_scope=True, SUCCESS.
#   - An empty / non-dict result -> gate rejects (False, reason).
#
# The S-3 gate function under test is run_security_gate (the shared helper the
# node AND OmnichannelClassificationAgent._extra_security_gate_output both call).
#
# S-4 audit: emit_trace_event is muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared package).

import pytest

from src.nodes.output_validate import OutputValidateNode, run_security_gate
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.output_validate.emit_trace_event",
        lambda *a, **k: None,
    )


def _valid_upstream_state(**overrides):
    """A complete, gate-passing upstream classification state."""
    base = {
        "channel_id": "ec",
        "channel_confidence": 1.0,
        "event_type": "purchase",
        "event_type_confidence": 1.0,
        "data_quality_score": 0.95,
        "data_quality_flags": [],
        "keihin_compliant": True,
        "keihin_violations": [],
        "appi_consent_status": "granted",
        "appi_flags": [],
        "routing_decision": "realtime",
        "routing_priority": 1,
        "out_of_scope": False,
    }
    base.update(overrides)
    return base


def _valid_result():
    """A fully assembled, gate-passing classification_result dict."""
    return {
        "channel_id": "ec",
        "channel_confidence": 1.0,
        "event_type": "purchase",
        "event_type_confidence": 1.0,
        "data_quality_score": 0.95,
        "data_quality_flags": [],
        "keihin_compliant": True,
        "keihin_violations": [],
        "appi_consent_status": "granted",
        "appi_flags": [],
        "routing_decision": "realtime",
        "routing_priority": 1,
        "out_of_scope": False,
    }


class TestOutputValidateNode:
    """Unit tests for OutputValidateNode.execute() (assemble + S-3 gate)."""

    def setup_method(self):
        self.node = OutputValidateNode()

    def test_happy_path_assembles_result(self):
        """Valid upstream state -> classification_result assembled, SUCCESS."""
        result = self.node.execute(_valid_upstream_state())

        assert result["status"] == AgentStatus.SUCCESS
        assert isinstance(result["classification_result"], dict)
        assert result["classification_result"]["routing_decision"] == "realtime"
        assert result["classification_result"]["out_of_scope"] is False

    def test_out_of_scope_is_valid_success_output(self):
        """out_of_scope=True -> valid output carrying out_of_scope=True, SUCCESS."""
        result = self.node.execute(
            _valid_upstream_state(
                out_of_scope=True,
                event_type="other",
                routing_decision="manual_review",
                routing_priority=2,
            )
        )

        assert result["status"] == AgentStatus.SUCCESS
        assert result["classification_result"]["out_of_scope"] is True


class TestRunSecurityGate:
    """Unit tests for the shared S-3 gate function run_security_gate()."""

    def test_clean_result_passes(self):
        """A complete, PII-clean result passes the gate -> (True, None)."""
        ok, reason = run_security_gate(_valid_result())

        assert ok is True
        assert reason is None

    def test_missing_required_key_rejected(self):
        """A result missing a required key is rejected -> (False, reason)."""
        result = _valid_result()
        del result["routing_decision"]
        ok, reason = run_security_gate(result)

        assert ok is False
        assert reason

    def test_pii_in_result_rejected(self):
        """An email/phone pattern anywhere in the result is rejected -> (False, reason)."""
        result = _valid_result()
        result["keihin_violations"] = ["leaked taro.yamada@example.com into the output"]
        ok, reason = run_security_gate(result)

        assert ok is False
        assert reason

    def test_disallowed_routing_decision_rejected(self):
        """A routing_decision outside the allow-list is rejected -> (False, reason)."""
        result = _valid_result()
        result["routing_decision"] = "auto_delete_everything"
        ok, reason = run_security_gate(result)

        assert ok is False
        assert reason

    def test_empty_result_rejected(self):
        """An empty / non-dict result is rejected -> (False, reason)."""
        ok, reason = run_security_gate({})

        assert ok is False
        assert reason

    def test_out_of_scope_result_still_passes(self):
        """out_of_scope=True is a VALID success state, not a gate failure."""
        result = _valid_result()
        result["out_of_scope"] = True
        result["routing_decision"] = "manual_review"
        ok, reason = run_security_gate(result)

        assert ok is True
        assert reason is None
