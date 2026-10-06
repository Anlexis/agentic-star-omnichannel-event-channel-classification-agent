"""AgentCore Platform v1.0"""

# The routing decision: realtime, batch, or manual review.
#
# A priority-ordered rule list, first match wins. The ordering is the policy:
# anything that could not be evaluated, or that failed a compliance rule, goes
# to a person before it goes anywhere else. Only an event that passed every
# check and carries enough quality to be trusted is allowed straight through.

import logging
from typing import Any, ClassVar, Dict, List, Mapping, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.caller_input import finite_in_range
from src.services.runtime_config import config_labels, config_number

logger = logging.getLogger(__name__)

_DEFAULT_QUALITY_THRESHOLD = 0.6
_DEFAULT_REALTIME_EVENT_TYPES = ("purchase", "complaint", "return")

REALTIME = "realtime"
BATCH = "batch"
MANUAL_REVIEW = "manual_review"

# Closed set of routing reasons. The reason travels into the audit record, so
# it is drawn from this fixed vocabulary rather than composed from state — a
# reason built by formatting state into a string is a way for caller data to
# reach the audit log. Every branch below assigns one of these literals, and
# tests/unit/test_routing_decide.py holds the emitted reasons to this set.
ROUTING_REASONS = frozenset(
    {
        "out_of_scope",
        "upstream_error",
        "unscored_quality",
        "keihin_non_compliant",
        "appi_consent_denied",
        "low_data_quality",
        "realtime_event",
        "default_batch",
    }
)


def _status_is_error(status_value: Any) -> bool:
    """True when the running status indicates an upstream error."""
    if status_value == AgentStatus.ERROR:
        return True
    return str(status_value) == str(AgentStatus.ERROR.value)


class RoutingDecideNode(FunctionNode):
    """Decide the final routing from upstream signals (deterministic rule engine).

    Priority-ordered, first match wins:
      1. out of scope                     -> manual_review, priority 2
      2. upstream error                   -> manual_review, priority 1
      3. quality score unreadable         -> manual_review, priority 1
      4. 景品表示法 non-compliant          -> manual_review, priority 1
      5. APPI consent denied              -> manual_review, priority 1
      6. quality below threshold          -> manual_review, priority 2
      7. realtime event type, quality met -> realtime,      priority 1
      8. otherwise                        -> batch,         priority 3

    Output (partial dict — only changed keys):
        routing_decision, routing_priority, out_of_scope, status, error_log.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: Optional[Mapping[str, Any]] = None) -> None:
        cfg = config or {}
        self._quality_threshold = config_number(cfg, "quality_threshold", _DEFAULT_QUALITY_THRESHOLD, 0.0, 1.0)
        self._realtime_event_types = frozenset(
            config_labels(cfg, "realtime_event_types", _DEFAULT_REALTIME_EVENT_TYPES)
        )

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        out_of_scope = bool(state.get("out_of_scope", False))
        keihin_compliant = state.get("keihin_compliant", True)
        appi_consent_status = state.get("appi_consent_status", "unknown")
        event_type = state.get("event_type", "other") or "other"

        # The score is produced upstream, but it is read here as a number that
        # must be finite before it is compared. An unreadable score routes to a
        # person: a comparison against NaN is False whichever way it is written,
        # so an unchecked score would quietly select the straight-through branch
        # by failing every test that would have diverted it.
        raw_score = state.get("data_quality_score", 0.0)
        data_quality_score = finite_in_range(raw_score, 0.0, 1.0)

        if out_of_scope:
            decision, priority, reason = MANUAL_REVIEW, 2, "out_of_scope"
        elif _status_is_error(state.get("status")):
            decision, priority, reason = MANUAL_REVIEW, 1, "upstream_error"
        elif data_quality_score is None:
            decision, priority, reason = MANUAL_REVIEW, 1, "unscored_quality"
        elif keihin_compliant is False:
            decision, priority, reason = MANUAL_REVIEW, 1, "keihin_non_compliant"
        elif appi_consent_status == "denied":
            decision, priority, reason = MANUAL_REVIEW, 1, "appi_consent_denied"
        elif data_quality_score < self._quality_threshold:
            decision, priority, reason = MANUAL_REVIEW, 2, "low_data_quality"
        elif event_type in self._realtime_event_types:
            decision, priority, reason = REALTIME, 1, "realtime_event"
        else:
            decision, priority, reason = BATCH, 3, "default_batch"

        logger.info(
            "RoutingDecideNode: event_type=%s quality=%s -> decision=%s priority=%d (reason=%s)",
            event_type,
            data_quality_score,
            decision,
            priority,
            reason,
        )

        emit_trace_event(
            "routing_decide_complete",
            {
                "event_type": event_type,
                "channel_id": state.get("channel_id"),
                "data_quality_score": data_quality_score,
                "keihin_compliant": keihin_compliant,
                "appi_consent_status": appi_consent_status,
                "routing_decision": decision,
                "routing_priority": priority,
                "reason": reason,
                "out_of_scope": out_of_scope,
            },
            state,
        )

        return {
            "routing_decision": decision,
            "routing_priority": priority,
            "out_of_scope": out_of_scope,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
