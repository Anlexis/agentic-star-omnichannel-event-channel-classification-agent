"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# RET-C2-252 — Omnichannel Data Integration & Channel Classification Agent
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# Domain flow (inner graph, linear):
#   EventParseChannelIdentify -> EventTypeClassify -> DataQualityScore
#   -> KeihinComplianceCheck -> APPIConsentCheck -> RoutingDecide -> OutputValidate
#
# Confidentiality / privacy note (S-4):
#   Raw customer PII (name, email, phone, address) is NEVER persisted in State.
#   EventParseChannelIdentifyNode masks PII at the input boundary; only the
#   masked `parsed_payload` is stored. keihin_violations / appi_flags /
#   data_quality_flags MUST contain product/price/compliance data only — no
#   customer identifiers. No credentials are stored in State (S-5).
#
# State-key contract (producer -> consumer) — single source of truth:
#   raw_input             (input)                              -> EventParseChannelIdentify
#   event_type_raw        EventParseChannelIdentify            -> EventTypeClassify, DataQualityScore
#   channel_id            EventParseChannelIdentify            -> EventTypeClassify, DataQualityScore,
#                                                                 KeihinComplianceCheck, APPIConsentCheck, RoutingDecide
#   channel_confidence    EventParseChannelIdentify            -> RoutingDecide
#   parsed_payload        EventParseChannelIdentify (masked)   -> DataQualityScore, KeihinComplianceCheck, APPIConsentCheck
#   event_type            EventTypeClassify                    -> DataQualityScore, KeihinComplianceCheck,
#                                                                 APPIConsentCheck, RoutingDecide
#   event_type_confidence EventTypeClassify                    -> RoutingDecide
#   data_quality_score    DataQualityScore                     -> RoutingDecide, OutputValidate
#   data_quality_flags    DataQualityScore                     -> RoutingDecide, OutputValidate
#   keihin_compliant      KeihinComplianceCheck                -> RoutingDecide, OutputValidate
#   keihin_violations     KeihinComplianceCheck                -> OutputValidate
#   appi_consent_status   APPIConsentCheck                     -> RoutingDecide, OutputValidate
#   appi_flags            APPIConsentCheck                     -> OutputValidate
#   routing_decision      RoutingDecide                        -> OutputValidate
#   routing_priority      RoutingDecide                        -> OutputValidate
#   out_of_scope          EventParseChannelIdentify/           -> all downstream + OutputValidate
#                         EventTypeClassify / RoutingDecide
#   classification_result OutputValidate                       -> (output / result)
#   status                every node (AgentStatus)             -> OutputValidate, graph
#   error_log             any node on error                    -> OutputValidate

from typing import Any, Dict, List, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat TypedDict for RET-C2-252.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState. The fields
    declared below are the RET-C2-252 domain extension.
    """

    # ------------------------------------------------------------------
    # Input boundary — EventParseChannelIdentifyNode (S-1 / S-4)
    # ------------------------------------------------------------------

    # Raw event payload supplied by the caller (JSON string or dict-as-string).
    # If it carries customer PII it is masked at the EventParseChannelIdentify
    # boundary BEFORE any other state key is written. Not persisted downstream.
    raw_input: Optional[str]

    # PII-masked parsed event payload produced by EventParseChannelIdentifyNode.
    # This is the ONLY representation of the event body stored in State; raw PII
    # fields (name/email/phone/address) are stripped/masked before it is written.
    # Read by DataQualityScore, KeihinComplianceCheck, APPIConsentCheck.
    parsed_payload: Optional[Dict[str, Any]]

    # Raw (pre-canonical) event type string parsed from the payload.
    # Read by EventTypeClassify (canonicalisation) and DataQualityScore
    # (field-expectation lookup).
    event_type_raw: Optional[str]

    # Canonical channel label: ec / store / sns / loyalty / app / call_center / other.
    channel_id: Optional[str]

    # Rule-based channel identification confidence, 0.0–1.0.
    channel_confidence: Optional[float]

    # ------------------------------------------------------------------
    # EventTypeClassifyNode
    # ------------------------------------------------------------------

    # Canonical event type: purchase / browse / return / complaint /
    # inquiry / inventory / promotion / other.
    event_type: Optional[str]

    # Rule-based event-type classification confidence, 0.0–1.0.
    event_type_confidence: Optional[float]

    # ------------------------------------------------------------------
    # DataQualityScoreNode
    # ------------------------------------------------------------------

    # Weighted data-quality score, 0.0–1.0.
    data_quality_score: Optional[float]

    # Human-readable, PII-masked descriptions of quality issues found.
    data_quality_flags: Optional[List[str]]

    # ------------------------------------------------------------------
    # KeihinComplianceCheckNode (景品表示法 cross-channel pricing consistency)
    # ------------------------------------------------------------------

    # True when cross-channel pricing / premium / loyalty rules pass.
    keihin_compliant: Optional[bool]

    # PII-masked violation descriptions (product/price data only — no customer
    # name/email/phone/address).
    keihin_violations: Optional[List[str]]

    # ------------------------------------------------------------------
    # APPIConsentCheckNode (改正個人情報保護法 per-channel consent)
    # ------------------------------------------------------------------

    # Consent status: "granted" / "denied" / "unknown".
    appi_consent_status: Optional[str]

    # PII-masked APPI compliance issues (flag the event type/channel, never the
    # individual — no direct customer identifier).
    appi_flags: Optional[List[str]]

    # ------------------------------------------------------------------
    # RoutingDecideNode
    # ------------------------------------------------------------------

    # Final routing decision: "realtime" / "batch" / "manual_review".
    routing_decision: Optional[str]

    # Routing priority: 1 (highest) – 3 (lowest).
    routing_priority: Optional[int]

    # ------------------------------------------------------------------
    # OutputValidateNode (S-3 gate, main result)
    # ------------------------------------------------------------------

    # Final assembled, PII-clean classification result dict written by
    # OutputValidateNode and surfaced to the caller. Passes the S-3 output gate
    # before being returned.
    classification_result: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Control / routing flags
    # ------------------------------------------------------------------

    # True when the channel or event type is unrecognised. Out-of-scope is NOT a
    # separate AgentStatus — status stays SUCCESS and OutputValidate / the outer
    # merge_output() check this flag to shape the caller response.
    out_of_scope: bool

    # ------------------------------------------------------------------
    # Status and audit — populated by all nodes
    # ------------------------------------------------------------------

    # AgentStatus string value ("SUCCESS" or "ERROR") set by each node.
    # Declared explicitly so the outer merge_output() can read it from the inner
    # graph's merged state without an attribute-access miss.
    status: Optional[str]

    # Accumulated error messages appended by any node that catches an exception
    # or rejects input/output. OutputValidate gates on this list.
    error_log: List[Any]

    # ------------------------------------------------------------------
    # Tracing / audit
    # ------------------------------------------------------------------

    # Correlation ID injected by the framework InitializeNode for audit-log
    # correlation. Every emit_trace_event() call carries it (via state).
    trace_id: Optional[str]
