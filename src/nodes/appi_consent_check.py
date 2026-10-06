"""AgentCore Platform v1.0"""

# 改正個人情報保護法 (APPI) — per-channel consent status.
#
# Consent granted on one channel does not carry to another. The check looks for
# an explicit signal in the event, falls back to the channel's declared default
# posture, and flags a consent that appears to have been carried across channels
# without a mapping that permits it.
#
# The posture defaults are deliberately "unknown" rather than "granted": an
# absent signal is an absent signal, and the alternative is an agent that
# reports consent it was never given.

import logging
from typing import Any, ClassVar, Dict, FrozenSet, List, Mapping, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.caller_input import render_identifier
from src.services.runtime_config import config_labels

logger = logging.getLogger(__name__)

# Payload field names that may carry an explicit consent signal.
_DEFAULT_CONSENT_FIELDS: List[str] = [
    "consent",
    "consent_flag",
    "appi_consent",
    "data_consent",
    "marketing_consent",
    "privacy_consent",
]

_GRANTED_TOKENS: FrozenSet[str] = frozenset(
    {"true", "1", "yes", "y", "granted", "agree", "agreed", "opt_in", "optin", "consented"}
)
_DENIED_TOKENS: FrozenSet[str] = frozenset(
    {"false", "0", "no", "n", "denied", "decline", "declined", "opt_out", "optout", "refused"}
)

_CANONICAL_CHANNELS: FrozenSet[str] = frozenset({"ec", "store", "sns", "loyalty", "app", "call_center", "other"})

UNKNOWN = "unknown"
GRANTED = "granted"
DENIED = "denied"

# A consent token is caller data compared against closed sets; bounded because
# it is read, lower-cased and looked up rather than trusted to be short.
_MAX_CONSENT_TOKEN_CHARS = 64


def _interpret_consent(value: Any) -> Optional[str]:
    """Interpret a raw consent value -> granted / denied / None (unparseable)."""
    if isinstance(value, bool):
        return GRANTED if value else DENIED
    token = str(value).strip().lower()[:_MAX_CONSENT_TOKEN_CHARS]
    if token in _GRANTED_TOKENS:
        return GRANTED
    if token in _DENIED_TOKENS:
        return DENIED
    return None


class APPIConsentCheckNode(FunctionNode):
    """改正個人情報保護法 per-channel consent-status check (deterministic).

    Output (partial dict — only changed keys):
        appi_consent_status, appi_flags, status, error_log.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, config: Optional[Mapping[str, Any]] = None) -> None:
        cfg = config or {}
        # Channels whose default posture is "granted" when the event carries no
        # explicit signal. Empty by default: declaring a channel here is an
        # assertion that consent was collected out of band for every event on
        # it, which is a decision for whoever operates the channel.
        self._presumed_consent_channels = frozenset(config_labels(cfg, "presumed_consent_channels", ()))
        # Pairs written "<source>:<target>" naming a source channel whose
        # consent may be applied to a target channel.
        self._cross_channel_consent = frozenset(config_labels(cfg, "cross_channel_consent", ()))

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])

        if state.get("out_of_scope"):
            logger.info("APPIConsentCheckNode: out_of_scope=True — consent unknown")
            return {
                "appi_consent_status": UNKNOWN,
                "appi_flags": [],
                "status": AgentStatus.SUCCESS,
                "error_log": error_log,
            }

        channel_id = state.get("channel_id", "other") or "other"
        event_type = state.get("event_type", "other") or "other"

        payload = state.get("parsed_payload") or {}
        if not isinstance(payload, dict):
            payload = {}

        flags: List[str] = []
        consent_status: Optional[str] = None

        # 1. Explicit consent signal.
        for field in _DEFAULT_CONSENT_FIELDS:
            if field in payload and payload[field] not in (None, ""):
                interpreted = _interpret_consent(payload[field])
                if interpreted is not None:
                    consent_status = interpreted
                    break
                # The field is named; the unparseable value is not echoed.
                flags.append(f"unparseable consent value in field '{field}' on channel '{channel_id}'")

        # 2. Channel default posture.
        if consent_status is None:
            if channel_id in self._presumed_consent_channels:
                consent_status = GRANTED
            else:
                consent_status = UNKNOWN
                flags.append(
                    f"no explicit APPI consent signal for channel '{channel_id}' "
                    f"(event type '{event_type}') — treated as unknown"
                )

        # 3. Cross-channel scope.
        #
        # The source channel is the caller's own string. It is rendered into a
        # flag, so it is held to an inert shape first — and separately narrowed
        # to a channel this agent recognises, because a source outside the
        # closed set cannot be checked against the mapping at all.
        raw_source = payload.get("consent_source_channel")
        if raw_source not in (None, ""):
            source = render_identifier(raw_source)
            normalised = source.strip().lower()
            if normalised not in _CANONICAL_CHANNELS:
                flags.append(
                    f"consent source channel '{source}' is not a channel this agent recognises — "
                    "cross-channel consent could not be verified"
                )
            elif normalised != str(channel_id) and f"{normalised}:{channel_id}" not in self._cross_channel_consent:
                flags.append(
                    f"consent appears sourced from channel '{normalised}' but is being applied "
                    f"to '{channel_id}' with no cross-channel mapping — APPI requires explicit "
                    "per-channel consent"
                )

        logger.info(
            "APPIConsentCheckNode: channel=%s event_type=%s status=%s flags=%d",
            channel_id,
            event_type,
            consent_status,
            len(flags),
        )

        emit_trace_event(
            "appi_consent_check_complete",
            {
                "channel_id": channel_id,
                "event_type": event_type,
                "appi_consent_status": consent_status,
                "flag_count": len(flags),
            },
            state,
        )

        return {
            "appi_consent_status": consent_status,
            "appi_flags": flags,
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
