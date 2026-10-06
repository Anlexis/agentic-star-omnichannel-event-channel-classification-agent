"""AgentCore Platform v1.0"""

# Outer backbone input stage — the first template code any request reaches.
#
# The request body arrives here as text: this agent's caller contract is a JSON
# object encoded as a string, decoded by the domain parser one layer in. That
# split is why this node screens the TEXT and the parser screens the DECODED
# OBJECT; see src/nodes/caller_input.py for why neither pass subsumes the other.
#
# The refusal is enforced here rather than left to the platform's own input
# gate. That gate is a property of the deployment: where it is absent or
# configured off, a payload that this node did not refuse reaches the answer
# path and comes back as a success. A template that only refuses when something
# in front of it happens to be running does not own its own guarantees.

from typing import Any, ClassVar, Dict, List, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.caller_input import MAX_RAW_INPUT_CHARS, find_control_token


class PreProcessNode(FunctionNode):
    """Validate the incoming request text before the domain pipeline sees it."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        error_log: List[str] = list(state.get("error_log") or [])
        user_input = state.get("user_input", "")

        if not isinstance(user_input, str) or not user_input.strip():
            emit_trace_event("input_refused", {"reason": "empty_input"}, state)
            return {
                "status": AgentStatus.ERROR,
                "error_log": error_log + ["PreProcessNode: request body is empty"],
            }

        if len(user_input) > MAX_RAW_INPUT_CHARS:
            emit_trace_event(
                "input_refused",
                {"reason": "input_too_large", "limit_chars": MAX_RAW_INPUT_CHARS},
                state,
            )
            return {
                "status": AgentStatus.ERROR,
                "error_log": error_log + [f"PreProcessNode: request body exceeds {MAX_RAW_INPUT_CHARS} characters"],
            }

        marker = find_control_token(user_input)
        if marker is not None:
            # The finding names the family, never the matched text. An error
            # message that quoted the marker back would carry the payload into
            # the log the refusal was meant to keep it out of.
            emit_trace_event("input_refused", {"reason": marker}, state)
            return {
                "status": AgentStatus.ERROR,
                "error_log": error_log + [f"PreProcessNode: request rejected — {marker}"],
            }

        return {
            "validated_input": user_input.strip(),
            "status": AgentStatus.SUCCESS,
            "error_log": error_log,
        }
