"""AgentCore Platform v1.0"""

# First node of the domain pipeline. Decodes the caller's event payload, removes
# personal data before anything is written to state, and identifies which
# channel the event came from.
#
# It is also the second of this agent's two input screens. The outer input stage
# saw the request as text; this one sees it decoded, which is the only place a
# marker written as a JSON escape becomes visible.

import json
import logging
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.caller_input import MAX_PAYLOAD_KEYS, MAX_RAW_INPUT_CHARS, screen_decoded
from src.nodes.redaction import mask_payload

logger = logging.getLogger(__name__)

# Canonical channel labels.
_CANONICAL_CHANNELS: FrozenSet[str] = frozenset({"ec", "store", "sns", "loyalty", "app", "call_center", "other"})

# Raw-token -> canonical-channel synonym map (lower-cased keys).
_CHANNEL_SYNONYMS: Dict[str, str] = {
    "ec": "ec",
    "ecommerce": "ec",
    "e-commerce": "ec",
    "online": "ec",
    "web": "ec",
    "webstore": "ec",
    "online_store": "ec",
    "store": "store",
    "shop": "store",
    "pos": "store",
    "retail": "store",
    "instore": "store",
    "in_store": "store",
    "offline": "store",
    "sns": "sns",
    "social": "sns",
    "instagram": "sns",
    "x": "sns",
    "twitter": "sns",
    "tiktok": "sns",
    "line": "sns",
    "facebook": "sns",
    "loyalty": "loyalty",
    "rewards": "loyalty",
    "membership": "loyalty",
    "points": "loyalty",
    "point": "loyalty",
    "app": "app",
    "mobile": "app",
    "mobile_app": "app",
    "ios": "app",
    "android": "app",
    "call_center": "call_center",
    "callcenter": "call_center",
    "call": "call_center",
    "phone_support": "call_center",
    "contact_center": "call_center",
    "support": "call_center",
}

# Payload keys that may carry the channel identifier (checked in order).
_CHANNEL_FIELD_CANDIDATES: List[str] = [
    "channel",
    "channel_id",
    "source",
    "source_channel",
    "origin",
    "touchpoint",
]

# Payload keys that may carry the (raw) event-type string.
_EVENT_TYPE_FIELD_CANDIDATES: List[str] = [
    "event_type",
    "type",
    "event",
    "action",
    "activity",
]

# The raw channel token is caller data. It is matched against a closed set and
# never rendered, but it is read repeatedly, so it is bounded here rather than
# trusted to be short.
_MAX_CHANNEL_TOKEN_CHARS = 64


def _coerce_payload(raw_input: Any) -> Optional[Dict[str, Any]]:
    """Decode raw_input into a dict payload. Returns None if it cannot be parsed."""
    if isinstance(raw_input, dict):
        return raw_input
    if isinstance(raw_input, str):
        text = raw_input.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict):
            return parsed
        # A bare JSON scalar/list is wrapped so downstream lookups stay safe.
        return {"value": parsed}
    return None


def _first_present(payload: Dict[str, Any], candidates: List[str]) -> Optional[str]:
    """Return the first candidate key's stringified value, if present and truthy."""
    for key in candidates:
        if key in payload and payload[key] not in (None, ""):
            return str(payload[key]).strip()
    return None


def _identify_channel(payload: Dict[str, Any]) -> Tuple[str, float, bool]:
    """Identify the canonical channel from the (masked) payload.

    Returns (channel_id, channel_confidence, out_of_scope).
    """
    raw_channel = _first_present(payload, _CHANNEL_FIELD_CANDIDATES)

    if raw_channel is None:
        # No channel field at all -> unrecognised / out of scope.
        return "other", 0.0, True

    token = raw_channel.lower().strip()[:_MAX_CHANNEL_TOKEN_CHARS]

    # Exact canonical hit.
    if token in _CANONICAL_CHANNELS and token != "other":
        return token, 1.0, False

    # Synonym hit.
    if token in _CHANNEL_SYNONYMS:
        return _CHANNEL_SYNONYMS[token], 0.9, False

    # Substring / fuzzy hit against synonym keys.
    for syn, canonical in _CHANNEL_SYNONYMS.items():
        if syn in token:
            return canonical, 0.7, False

    # Recognised as a value but not mappable -> out of scope.
    return "other", 0.3, True


class EventParseChannelIdentifyNode(FunctionNode):
    """Parse the event payload, remove personal data, and identify the channel.

    Output (partial dict — only changed keys):
        parsed_payload, event_type_raw, channel_id, channel_confidence,
        out_of_scope, status, error_log.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        raw_input = state.get("raw_input")
        if raw_input is None:
            raw_input = state.get("user_input")

        if not isinstance(raw_input, (str, dict)) or (isinstance(raw_input, str) and not raw_input.strip()):
            return self._refuse(
                state,
                error_log,
                "missing_payload",
                "raw_input is absent, empty, or not a string/dict — cannot parse event payload",
            )

        if isinstance(raw_input, str) and len(raw_input) > MAX_RAW_INPUT_CHARS:
            return self._refuse(
                state,
                error_log,
                "payload_too_large",
                f"event payload exceeds {MAX_RAW_INPUT_CHARS} characters",
            )

        payload = _coerce_payload(raw_input)
        if payload is None:
            return self._refuse(
                state,
                error_log,
                "undecodable_payload",
                "event payload could not be decoded as a JSON object",
            )

        if len(payload) > MAX_PAYLOAD_KEYS:
            return self._refuse(
                state,
                error_log,
                "payload_too_many_fields",
                f"event payload declares more than {MAX_PAYLOAD_KEYS} fields",
            )

        # Second input screen — over the DECODED payload, keys included. A
        # marker spelled with JSON escapes is inert in the request text and
        # plain here, so this is the pass that sees it.
        marker = screen_decoded(payload)
        if marker is not None:
            return self._refuse(state, error_log, marker, f"event payload rejected — {marker}")

        # Personal data is removed BEFORE any state key is written, so nothing
        # downstream can read a value that was never supposed to be persisted.
        masked_payload = mask_payload(payload)

        event_type_raw = _first_present(masked_payload, _EVENT_TYPE_FIELD_CANDIDATES) or ""
        channel_id, channel_confidence, out_of_scope = _identify_channel(masked_payload)

        logger.info(
            "EventParseChannelIdentifyNode: channel=%s confidence=%.2f out_of_scope=%s",
            channel_id,
            channel_confidence,
            out_of_scope,
        )

        emit_trace_event(
            "event_parse_channel_identify_complete",
            {
                "channel_id": channel_id,
                "channel_confidence": channel_confidence,
                "out_of_scope": out_of_scope,
                "payload_key_count": len(masked_payload),
            },
            state,
        )

        return {
            "parsed_payload": masked_payload,
            "event_type_raw": event_type_raw,
            "channel_id": channel_id,
            "channel_confidence": channel_confidence,
            "out_of_scope": out_of_scope,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }

    @staticmethod
    def _refuse(state: Dict[str, Any], error_log: List[str], reason: str, message: str) -> Dict[str, Any]:
        """Refuse the request naming the reason, never the rejected value."""
        emit_trace_event("event_payload_refused", {"reason": reason}, state)
        return {
            "status": AgentStatus.ERROR,
            "error_log": error_log + [f"EventParseChannelIdentifyNode: {message}"],
        }
