"""AgentCore Platform v1.0"""

# Data-quality scoring of the parsed event.
#
# Three measures, weighted: how many of the fields this event type needs are
# present, how many of the present fields are well formed, and how much of the
# payload carries a value at all. The score is what the router uses to decide
# whether an event can be trusted to flow straight through.
#
# The format rules are the same ones the rest of the pipeline uses to decide
# what an identifier is, which matters more than it sounds: if the scorer called
# a SKU malformed and the renderer called the same SKU fine, the score would be
# measuring the disagreement rather than the data.

import logging
import re
from typing import Any, ClassVar, Dict, List, Mapping, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.caller_input import finite_in_range, inert_identifier, is_redacted
from src.services.runtime_config import config_number

logger = logging.getLogger(__name__)

# Required fields per canonical event type.
_REQUIRED_FIELDS_BY_EVENT: Dict[str, List[str]] = {
    "purchase": ["event_id", "timestamp", "sku", "amount"],
    "return": ["event_id", "timestamp", "sku", "amount"],
    "browse": ["event_id", "timestamp", "sku"],
    "complaint": ["event_id", "timestamp"],
    "inquiry": ["event_id", "timestamp"],
    "inventory": ["event_id", "timestamp", "sku"],
    "promotion": ["event_id", "timestamp", "sku"],
    "other": ["event_id", "timestamp"],
}

_BASELINE_FIELDS: List[str] = ["event_id", "timestamp"]

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+\-]\d{2}:?\d{2})?$")

_W_REQUIRED = 0.5
_W_FORMAT = 0.3
_W_COMPLETENESS = 0.2

_DEFAULT_AMOUNT_MIN = 0.0
_DEFAULT_AMOUNT_MAX = 100_000_000.0


def _is_valid_timestamp(value: Any) -> bool:
    return isinstance(value, str) and bool(_TIMESTAMP_RE.match(value.strip()))


class DataQualityScoreNode(FunctionNode):
    """Score the data quality of the parsed event payload (deterministic).

    Score = 0.5 * required_field_ratio
          + 0.3 * format_validity_ratio
          + 0.2 * completeness_ratio

    Output (partial dict — only changed keys):
        data_quality_score, data_quality_flags, status, error_log.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: Optional[Mapping[str, Any]] = None) -> None:
        cfg = config or {}
        self._amount_min = config_number(cfg, "amount_min", _DEFAULT_AMOUNT_MIN, 0.0, 1_000_000_000_000.0)
        self._amount_max = config_number(cfg, "amount_max", _DEFAULT_AMOUNT_MAX, 0.0, 1_000_000_000_000.0)

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        if state.get("out_of_scope"):
            logger.info("DataQualityScoreNode: out_of_scope=True — default score 0.0")
            return {
                "data_quality_score": 0.0,
                "data_quality_flags": [],
                "status": AgentStatus.SUCCESS,
                "error_log": error_log,
            }

        payload: Dict[str, Any] = state.get("parsed_payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        event_type = state.get("event_type", "other") or "other"
        required = _REQUIRED_FIELDS_BY_EVENT.get(event_type, _BASELINE_FIELDS)

        # Flags name FIELDS and the rule they broke. The offending value is
        # never included: these strings are returned to the caller, and a
        # quality report that quotes the malformed value back is a channel for
        # whatever the value happened to contain.
        flags: List[str] = []

        # 1. Required-field presence.
        present = 0
        for field in required:
            if field in payload and payload[field] not in (None, ""):
                present += 1
            else:
                flags.append(f"missing required field '{field}' for event type '{event_type}'")
        required_ratio = present / len(required) if required else 1.0

        # 2. Format validity over the format-checkable fields present.
        format_checks: List[bool] = []

        if payload.get("timestamp") not in (None, ""):
            ok = _is_valid_timestamp(payload["timestamp"])
            format_checks.append(ok)
            if not ok:
                flags.append("field 'timestamp' is not a valid ISO-8601 datetime")

        for id_field in ("event_id", "sku", "customer_ref"):
            if payload.get(id_field) not in (None, ""):
                ok = inert_identifier(payload[id_field]) is not None
                format_checks.append(ok)
                if not ok:
                    # A redacted field and a malformed one are different facts
                    # and only one of them is the caller's to fix. The platform's
                    # personal-data filter rewrites what it matches before this
                    # code runs, and a retail reference that embeds a date can
                    # match its Japanese identity-number shape — so reporting
                    # that as a formatting fault would blame the caller for a
                    # value the platform replaced. Either way the field is not
                    # certified: a redaction marker is never scored as data.
                    if is_redacted(payload[id_field]):
                        flags.append(
                            f"field '{id_field}' was redacted before classification and " "could not be verified"
                        )
                    else:
                        flags.append(f"field '{id_field}' has an invalid identifier format")

        if payload.get("amount") not in (None, ""):
            ok = finite_in_range(payload["amount"], self._amount_min, self._amount_max) is not None
            format_checks.append(ok)
            if not ok:
                flags.append("field 'amount' is not a finite number within the configured bounds")

        format_ratio = sum(1 for check in format_checks if check) / len(format_checks) if format_checks else 1.0

        # 3. Completeness.
        total_keys = len(payload)
        non_empty = sum(1 for value in payload.values() if value not in (None, "", [], {}))
        completeness_ratio = (non_empty / total_keys) if total_keys else 0.0
        if total_keys == 0:
            flags.append("event payload is empty")

        score = _W_REQUIRED * required_ratio + _W_FORMAT * format_ratio + _W_COMPLETENESS * completeness_ratio
        score = round(max(0.0, min(1.0, score)), 4)

        logger.info(
            "DataQualityScoreNode: event_type=%s score=%.4f flags=%d",
            event_type,
            score,
            len(flags),
        )

        emit_trace_event(
            "data_quality_score_complete",
            {
                "event_type": event_type,
                "data_quality_score": score,
                "flag_count": len(flags),
                "required_ratio": round(required_ratio, 4),
                "format_ratio": round(format_ratio, 4),
                "completeness_ratio": round(completeness_ratio, 4),
            },
            state,
        )

        return {
            "data_quality_score": score,
            "data_quality_flags": flags,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
