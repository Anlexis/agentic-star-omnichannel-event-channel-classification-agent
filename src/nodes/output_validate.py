"""AgentCore Platform v1.0"""

# The output boundary. Assembles the classification result and decides whether
# it may be released.
#
# What this agent promises about its response is four things, and the gate below
# is where each of them stops being a claim:
#
#   1. no personal data — no address and no telephone number survives into it;
#   2. no credential-shaped value;
#   3. a routing decision from the closed set, with every declared field present;
#   4. no raw monetary line item — the response carries ratios and the identity
#      of the rule that was breached, never the amounts the caller sent.
#
# The gate SCANS; it does not rewrite. A boundary that quietly repairs its own
# output cannot tell anyone that the layer in front of it failed, and the next
# reader of the code has no way to know the repair is load-bearing. Masking is
# the input boundary's job and it has already run; if anything reached here, the
# honest answer is a refusal.
#
# On a refusal every output-bearing field is written back present and empty.
# Node results are MERGED into state, so a field that is merely left out of the
# refusal keeps the value it already had — and the framework's projection falls
# back to `result` even on an error status, which is exactly the field that
# would still be holding the refused content.

import logging
import re
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value
from shared.utils.audit_logger import emit_trace_event

from src.nodes.redaction import collect_strings, find_pii

logger = logging.getLogger(__name__)

# Allowed routing decisions.
ALLOWED_ROUTING_DECISIONS: FrozenSet[str] = frozenset({"realtime", "batch", "manual_review"})

# Required keys in the assembled classification_result.
REQUIRED_RESULT_KEYS: Tuple[str, ...] = (
    "channel_id",
    "channel_confidence",
    "event_type",
    "event_type_confidence",
    "data_quality_score",
    "data_quality_flags",
    "keihin_compliant",
    "keihin_violations",
    "appi_consent_status",
    "appi_flags",
    "routing_decision",
    "routing_priority",
    "out_of_scope",
)

# ── Credential detection ─────────────────────────────────────────────────────
# The framework's detector is the FLOOR, never the whole check. Its patterns
# describe credential FORMATS — `AKIA…`, `sk_live_…`, a JWT, a database URI —
# and this template's own rule below describes a credential HABIT: a secret
# written next to its name. Neither is a superset of the other.
#
# Taking the union rather than choosing between them matters in both
# directions. Dropping the framework's patterns would leave a value it catches
# to be found instead inside the framework's own gate, which raises there and
# discards this node's clearing — a detector gap is a containment bypass.
# Dropping the local pattern to "delegate to the framework" looks like a
# tightening and is the opposite: `password=hunter2hunter2` matches no
# credential format and would sail through.
_INLINE_SECRET_RE = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)" r"\s*[:=]\s*\S{6,}"
)


def find_credential(obj: Any) -> Optional[str]:
    """Return the kind of credential found anywhere in *obj*, else None.

    The KIND is returned and never the value — this explains a refusal, and a
    refusal quoting the secret it refused has published it.
    """
    findings = detect_credentials_in_value(obj)
    if findings:
        return str(findings[0].get("type", "credential"))
    for text in collect_strings(obj):
        if _INLINE_SECRET_RE.search(text):
            return "inline_secret_assignment"
    return None


def assemble_result(state: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the classification_result dict from upstream State keys."""
    return {
        "channel_id": state.get("channel_id", "other"),
        "channel_confidence": state.get("channel_confidence", 0.0),
        "event_type": state.get("event_type", "other"),
        "event_type_confidence": state.get("event_type_confidence", 0.0),
        "data_quality_score": state.get("data_quality_score", 0.0),
        "data_quality_flags": state.get("data_quality_flags", []) or [],
        "keihin_compliant": state.get("keihin_compliant", True),
        "keihin_violations": state.get("keihin_violations", []) or [],
        "appi_consent_status": state.get("appi_consent_status", "unknown"),
        "appi_flags": state.get("appi_flags", []) or [],
        "routing_decision": state.get("routing_decision", "manual_review"),
        "routing_priority": state.get("routing_priority", 2),
        "out_of_scope": bool(state.get("out_of_scope", False)),
    }


def run_security_gate(result: Any) -> Tuple[bool, Optional[str]]:
    """Decide whether *result* may be released. Returns (ok, reason).

    Shared by OutputValidateNode, PostProcessNode, and the outer graph's
    output hook, so the three cannot disagree about what is releasable.

    The reason is drawn from a closed vocabulary. A reason derived from the
    offending value would carry that value back out through the refusal.
    """
    if not result or not isinstance(result, dict):
        return False, "result_absent"

    missing = [key for key in REQUIRED_RESULT_KEYS if key not in result]
    if missing:
        return False, "result_incomplete"

    if result.get("routing_decision") not in ALLOWED_ROUTING_DECISIONS:
        return False, "routing_decision_not_allowed"

    # Scanned BEFORE anything else touches the strings, and over the nested
    # structure rather than its top level: the flags and violations are lists
    # of strings one level down, which is where every caller-derived word in
    # this response lives.
    pii_kind = find_pii(result)
    if pii_kind is not None:
        return False, f"personal_data_{pii_kind}"

    credential_kind = find_credential(result)
    if credential_kind is not None:
        return False, f"credential_{credential_kind}"

    return True, None


class OutputValidateNode(FunctionNode):
    """Assemble the classification result and enforce the output boundary.

    An out-of-scope event is a valid success: the result is complete, it simply
    says the event was not one this agent classifies.

    Output (partial dict — only changed keys):
        classification_result, result, status, error_log.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        result = assemble_result(state)

        ok, reason = run_security_gate(result)
        if not ok:
            logger.error("OutputValidateNode: output withheld — %s", reason)
            emit_trace_event("output_security_violation", {"reason": reason}, state)
            # Present and empty, not omitted — see the module note.
            return {
                "classification_result": None,
                "result": None,
                "status": AgentStatus.ERROR,
                "error_log": error_log + [f"OutputValidateNode: output withheld — {reason}"],
            }

        emit_trace_event(
            "classification_complete",
            {
                "channel_id": result["channel_id"],
                "event_type": result["event_type"],
                "routing_decision": result["routing_decision"],
                "routing_priority": result["routing_priority"],
                "data_quality_score": result["data_quality_score"],
                "keihin_compliant": result["keihin_compliant"],
                "appi_consent_status": result["appi_consent_status"],
                "out_of_scope": result["out_of_scope"],
            },
            state,
        )

        logger.info(
            "OutputValidateNode: released — channel=%s event_type=%s routing=%s",
            result["channel_id"],
            result["event_type"],
            result["routing_decision"],
        )

        return {
            "classification_result": result,
            "result": result,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
