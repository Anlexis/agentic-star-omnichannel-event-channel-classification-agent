# RET-C2-252 — Proof-of-Boundary: S-3 output gate enforcement
#
# The S-3 mandatory output security gate for RET-C2-252 lives in the module-level
# run_security_gate() helper (output_validate.py). It is invoked from BOTH:
#   - OutputValidateNode.execute() — converts a gate failure to status=ERROR; and
#   - OmnichannelClassificationAgent._extra_security_gate_output (graph.py, the
#     ADR-017 framework hook) — returns (ok, reason) to the framework pipeline.
#
# PoB scenarios (asserted against the MERGED develop implementation, 911c1ead):
#   PoB-1  PII in the assembled classification_result -> the gate returns
#          (False, reason); the outer hook surfaces it as a gate failure.
#   PoB-2  A valid, PII-clean output passes the S-3 gate (True, None).
#   PoB-3  An empty classification_result is blocked by the S-3 gate (False, reason).
#
# The gate cannot be bypassed: a result that is empty, missing required keys, or
# carries customer PII is rejected at the boundary.
#
# S-4 audit: emit_trace_event is muted at each node module under test via an
# autouse fixture (NEVER stub shared.* in sys.modules — the CI wheel ships a real
# shared package).

import pytest

from src.nodes.output_validate import OutputValidateNode, run_security_gate
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at each node module under test."""
    monkeypatch.setattr("src.nodes.output_validate.emit_trace_event", lambda *a, **k: None)


def _clean_result():
    """A complete, PII-clean classification_result that must pass the S-3 gate."""
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


class TestPoBS3GateRejectsPII:
    """PoB-1: the S-3 gate cannot be bypassed — PII in the output fails."""

    def test_gate_rejects_pii_in_result(self):
        """An email pattern injected into the result -> run_security_gate (False, reason)."""
        result = _clean_result()
        result["keihin_violations"] = ["price mismatch for taro.yamada@example.com"]
        ok, reason = run_security_gate(result)

        assert ok is False
        assert reason

    def test_outer_hook_surfaces_pii_as_gate_failure(self):
        """The ADR-017 hook _extra_security_gate_output reports the PII failure."""
        try:
            from src.graph.graph import OmnichannelClassificationAgent
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        agent = OmnichannelClassificationAgent()
        result = _clean_result()
        result["appi_flags"] = ["contact 090-1234-5678 for consent details"]
        ok, reason = agent._extra_security_gate_output({"classification_result": result})

        assert ok is False
        assert reason


class TestPoBS3GatePassesValidOutput:
    """PoB-2: a valid, PII-clean output passes the S-3 gate."""

    def test_gate_passes_clean_result(self):
        """A complete, clean result passes run_security_gate -> (True, None)."""
        ok, reason = run_security_gate(_clean_result())

        assert ok is True
        assert reason is None

    def test_output_validate_node_success_on_clean_state(self):
        """OutputValidateNode.execute() emits classification_result with SUCCESS."""
        node = OutputValidateNode()
        # The upstream state keys mirror _clean_result() (assemble_result reads them).
        result = node.execute(_clean_result())

        assert result["status"] == AgentStatus.SUCCESS
        assert result["classification_result"]["routing_decision"] == "realtime"


class TestPoBS3GateBlocksEmptyResult:
    """PoB-3: an empty classification_result is blocked by the S-3 gate."""

    def test_gate_rejects_empty_dict(self):
        """An empty classification_result -> run_security_gate (False, reason)."""
        ok, reason = run_security_gate({})

        assert ok is False
        assert reason

    def test_outer_hook_rejects_missing_result(self):
        """The ADR-017 hook rejects a None / absent classification_result."""
        try:
            from src.graph.graph import OmnichannelClassificationAgent
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        agent = OmnichannelClassificationAgent()
        ok, reason = agent._extra_security_gate_output({})

        assert ok is False
        assert reason
