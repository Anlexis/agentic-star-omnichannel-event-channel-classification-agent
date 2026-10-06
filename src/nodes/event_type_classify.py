"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state, config=None) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-252 — EventTypeClassifyNode
# Inner DomainWorkflowGraph node 2. Maps the raw event-type string + channel
# to a canonical event type with a rule-based confidence score.
# Deterministic keyword / pattern matching — no LLM in v1.
#
# Reads:  event_type_raw, channel_id, out_of_scope
# Writes: event_type, event_type_confidence, out_of_scope, status, error_log

import logging
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# Canonical event types.
_CANONICAL_EVENT_TYPES: FrozenSet[str] = frozenset(
    {
        "purchase",
        "browse",
        "return",
        "complaint",
        "inquiry",
        "inventory",
        "promotion",
        "other",
    }
)

# Canonical -> keyword/synonym tokens (lower-cased). Order matters: the first
# canonical type whose token set matches wins for an exact-token hit.
_EVENT_TYPE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "purchase": ("purchase", "buy", "order", "checkout", "payment", "transaction", "sale", "purchased"),
    "return": ("return", "refund", "exchange", "rma", "cancel_order", "returned"),
    "complaint": ("complaint", "complain", "dispute", "escalation", "grievance"),
    "inquiry": ("inquiry", "enquiry", "question", "support", "contact", "ask", "request_info"),
    "browse": ("browse", "view", "pageview", "page_view", "search", "click", "impression", "visit", "viewed"),
    "inventory": ("inventory", "stock", "restock", "stockout", "replenish", "sku_update"),
    "promotion": ("promotion", "promo", "campaign", "coupon", "discount", "offer", "deal"),
}


# An event type is a single word. Bounding the token keeps the cost of a
# classification a function of the vocabulary rather than of what the caller
# chose to send in the field.
_MAX_EVENT_TYPE_TOKEN_CHARS = 64


def _classify_event_type(event_type_raw: str) -> Tuple[str, float]:
    """Map a raw event-type string to a canonical type + confidence.

    Returns ("other", 0.0) when no token matches.
    """
    # Bounded before use. The token is caller data and is compared against
    # every keyword in the table; an event type is a short word, so anything
    # past this length carries no additional information to classify on.
    token = (event_type_raw or "").strip().lower()[:_MAX_EVENT_TYPE_TOKEN_CHARS]
    if not token:
        return "other", 0.0

    # Exact canonical match.
    if token in _CANONICAL_EVENT_TYPES and token != "other":
        return token, 1.0

    # Exact keyword/synonym match.
    for canonical, keywords in _EVENT_TYPE_KEYWORDS.items():
        if token in keywords:
            return canonical, 0.95

    # Substring match against keywords (lower confidence).
    for canonical, keywords in _EVENT_TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in token or token in kw:
                return canonical, 0.7

    return "other", 0.0


class EventTypeClassifyNode(FunctionNode):
    """Classify the canonical event type from event_type_raw + channel_id.

    Deterministic keyword lookup — no LLM in v1.

    Short-circuit: if `out_of_scope` is already True (set by
    EventParseChannelIdentifyNode), classification is skipped and the flag is
    preserved.

    Output (partial dict — only changed keys):
        event_type, event_type_confidence, out_of_scope, status, error_log.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        # Short-circuit: already out of scope upstream -> pass through.
        if state.get("out_of_scope"):
            logger.info("EventTypeClassifyNode: out_of_scope=True upstream — skipping classification")
            return {
                "event_type": "other",
                "event_type_confidence": 0.0,
                "out_of_scope": True,
                "status": AgentStatus.SUCCESS,
                "error_log": error_log,
            }

        event_type_raw = state.get("event_type_raw", "") or ""
        channel_id = state.get("channel_id", "other")

        event_type, confidence = _classify_event_type(event_type_raw)

        # Unrecognised event -> out of scope (do not unset an existing True).
        out_of_scope = event_type == "other"

        logger.info(
            "EventTypeClassifyNode: channel=%s event_type_raw=%s -> event_type=%s " "confidence=%.2f out_of_scope=%s",
            channel_id,
            event_type_raw or "<none>",
            event_type,
            confidence,
            out_of_scope,
        )

        emit_trace_event(
            "event_type_classify_complete",
            {
                "channel_id": channel_id,
                "event_type": event_type,
                "event_type_confidence": confidence,
                "out_of_scope": out_of_scope,
            },
            state,
        )

        return {
            "event_type": event_type,
            "event_type_confidence": confidence,
            "out_of_scope": out_of_scope,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
