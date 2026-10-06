"""AgentCore Platform v1.0"""

# 景品表示法 — cross-channel pricing consistency.
#
# Three rules, each a ratio against a declared ceiling: a SKU's price against
# its cross-channel reference, a premium's value against the transaction it is
# attached to, and a loyalty-point scheme's conversion rate.
#
# Every number the rules compare comes from the caller, and every one of them
# goes through a finite parser first. That is not defensive tidiness: a ratio
# built from a NaN compares False against its ceiling, so the rule returns
# "compliant" for the one input a pricing-compliance check most needs to
# refuse — and it does so with no error, no flag, and a successful response.

import logging
from typing import Any, ClassVar, Dict, FrozenSet, List, Mapping, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.caller_input import finite_in_range, render_identifier
from src.services.runtime_config import config_number

logger = logging.getLogger(__name__)

# Event types for which 景品表示法 pricing rules are in scope.
_IN_SCOPE_EVENT_TYPES: FrozenSet[str] = frozenset({"purchase", "promotion", "browse"})

# Defaults — April 2026 digital-extension rules. Each is overridable from
# `config/config.yaml` under `domain:`.
_DEFAULT_PRICE_DISCREPANCY_RATIO = 0.20
_DEFAULT_PREMIUM_RATE_MAX = 0.20
_DEFAULT_LOYALTY_POINT_RATE_MAX = 0.50

# Bounds on caller-supplied monetary values. A transaction outside these is not
# a large transaction, it is a malformed one.
MONETARY_MIN = 0.0
MONETARY_MAX = 1_000_000_000_000.0


def _amount(payload: Mapping[str, Any], *keys: str) -> Optional[float]:
    """First present key's value as a finite, bounded number, else None."""
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return finite_in_range(payload[key], MONETARY_MIN, MONETARY_MAX)
    return None


def _sku_label(payload: Mapping[str, Any]) -> str:
    """A product label safe to render into a violation message.

    The value is the caller's own, and violations are the part of this
    response a person reads, so it is held to an inert identifier shape. A SKU
    carrying a newline would otherwise arrive in a consumer's rendering of the
    violation list as an extra line that no rule produced.
    """
    for key in ("sku", "product_id", "item_id", "product_code"):
        value = payload.get(key)
        if value not in (None, ""):
            return render_identifier(value)
    return "<unknown_sku>"


class KeihinComplianceCheckNode(FunctionNode):
    """景品表示法 cross-channel pricing-consistency check (deterministic).

    Short-circuit: out of scope, or a non-pricing event type, returns
    compliant with no violations.

    Output (partial dict — only changed keys):
        keihin_compliant, keihin_violations, status, error_log.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: Optional[Mapping[str, Any]] = None) -> None:
        cfg = config or {}
        # Resolved once, at construction. `config` is not a parameter of
        # execute() that anything supplies — the framework calls node(state)
        # with the state alone — so a threshold read at call time from an
        # argument that is always None is a threshold that is never applied.
        self._price_discrepancy_ratio = config_number(
            cfg, "price_discrepancy_ratio", _DEFAULT_PRICE_DISCREPANCY_RATIO, 0.0, 100.0
        )
        self._premium_rate_max = config_number(cfg, "premium_rate_max", _DEFAULT_PREMIUM_RATE_MAX, 0.0, 100.0)
        self._loyalty_point_rate_max = config_number(
            cfg, "loyalty_point_rate_max", _DEFAULT_LOYALTY_POINT_RATE_MAX, 0.0, 100.0
        )

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])
        event_type = state.get("event_type", "other") or "other"

        if state.get("out_of_scope") or event_type not in _IN_SCOPE_EVENT_TYPES:
            logger.info(
                "KeihinComplianceCheckNode: skipped (out_of_scope=%s, event_type=%s)",
                state.get("out_of_scope"),
                event_type,
            )
            return {
                "keihin_compliant": True,
                "keihin_violations": [],
                "status": AgentStatus.SUCCESS,
                "error_log": error_log,
            }

        payload = state.get("parsed_payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        channel_id = state.get("channel_id", "other")
        sku = _sku_label(payload)
        violations: List[str] = []
        # Values the caller sent that could not be read as finite numbers. The
        # FIELD is named; the value never is. A rule that cannot be evaluated is
        # reported rather than skipped, because "no violation found" and "the
        # check did not run" are different answers and only one of them is safe
        # to treat as compliant.
        unevaluated: List[str] = []

        price = _amount(payload, "price", "amount")
        reference_price = _amount(payload, "reference_price", "cross_channel_price")
        transaction_value = _amount(payload, "amount", "price", "transaction_value")
        premium_value = _amount(payload, "premium_value", "gift_value")
        points_value = _amount(payload, "loyalty_points_value", "points_value")

        for field in (
            "price",
            "amount",
            "reference_price",
            "cross_channel_price",
            "transaction_value",
            "premium_value",
            "gift_value",
            "loyalty_points_value",
            "points_value",
        ):
            if (
                payload.get(field) not in (None, "")
                and finite_in_range(payload[field], MONETARY_MIN, MONETARY_MAX) is None
            ):
                unevaluated.append(field)

        # Rule 1 — cross-channel price consistency.
        if price is not None and reference_price is not None and reference_price > 0:
            discrepancy = abs(price - reference_price) / reference_price
            if discrepancy > self._price_discrepancy_ratio:
                violations.append(
                    f"cross-channel price discrepancy for SKU {sku} on channel "
                    f"'{channel_id}': {discrepancy:.1%} exceeds the "
                    f"{self._price_discrepancy_ratio:.0%} 景品表示法 threshold"
                )

        # Rule 2 — excessive premium (景品) rate.
        if premium_value is not None and transaction_value is not None and transaction_value > 0:
            premium_rate = premium_value / transaction_value
            if premium_rate > self._premium_rate_max:
                violations.append(
                    f"excessive premium for SKU {sku}: premium rate {premium_rate:.1%} "
                    f"exceeds the {self._premium_rate_max:.0%} 総付景品 ceiling"
                )

        # Rule 3 — loyalty-point manipulation.
        if points_value is not None and transaction_value is not None and transaction_value > 0:
            point_rate = points_value / transaction_value
            if point_rate > self._loyalty_point_rate_max:
                violations.append(
                    f"suspicious loyalty-point scheme for SKU {sku}: point-to-value "
                    f"rate {point_rate:.1%} exceeds the {self._loyalty_point_rate_max:.0%} threshold"
                )

        for field in unevaluated:
            violations.append(
                f"field '{field}' could not be read as a finite monetary value for SKU {sku} — "
                "the 景品表示法 rule that depends on it was not evaluated"
            )

        keihin_compliant = len(violations) == 0

        logger.info(
            "KeihinComplianceCheckNode: channel=%s compliant=%s violations=%d unevaluated=%d",
            channel_id,
            keihin_compliant,
            len(violations),
            len(unevaluated),
        )

        emit_trace_event(
            "keihin_compliance_check_complete",
            {
                "channel_id": channel_id,
                "event_type": event_type,
                "keihin_compliant": keihin_compliant,
                "violation_count": len(violations),
                "unevaluated_field_count": len(unevaluated),
            },
            state,
        )

        return {
            "keihin_compliant": keihin_compliant,
            "keihin_violations": violations,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
