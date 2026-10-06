"""AgentCore Platform v1.0"""

# Outer backbone output stage. Runs after the domain pipeline has produced the
# classification result and carried it back through the main slot.
#
# Trust level: every node declares one explicitly — the framework refuses to
# define a class that leaves it implicit. The value here is the manifest's
# `required_trust_level`, the same as every other node in this agent. A node
# above that value would deny the very callers the manifest invites, and one
# below it would answer callers the manifest excludes;
# tests/integration/test_manifest_identity_alignment.py holds the whole node set
# to the manifest rather than to a value restated here.

from typing import Any, ClassVar, Dict, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.output_validate import run_security_gate

# Returned to the caller in place of a result the output boundary refused.
# Deliberately a non-empty string: `AgentBaseGraph.get_output` returns
# `formatted_output or result`, so a falsy replacement re-opens the very
# fallback this withholding exists to close, and the ungated inner answer
# ships inside the error envelope.
WITHHELD_NOTICE = "The classification result was withheld because it did not pass the output boundary."

# Closed set of refusal labels. The reason a value was refused can be derived
# from that value, so a free-form reason is a channel back out for the content
# that was just withheld.
_WITHHELD_REASON = "output_boundary_violation"


class PostProcessNode(FunctionNode):
    """Surface the classification result, or withhold it and say so.

    The domain pipeline's own output boundary (`OutputValidateNode`) is the
    first enforcement point. This node is the second, independent one: it is the
    last place the result passes through before the framework projects it to the
    caller, and it is the only place that can clear the state fields the
    projection falls back to.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        result = state.get("result")

        ok, reason = run_security_gate(result)
        # `run_security_gate` refuses anything that is not a populated mapping,
        # so a pass narrows the type as well as clearing the content. The
        # isinstance check states that for a reader and a type checker both,
        # rather than leaving the guarantee implicit in another module.
        if not ok or not isinstance(result, dict):
            emit_trace_event(
                "output_withheld",
                {"reason": _WITHHELD_REASON, "detail": reason},
                state,
            )
            # Every output-bearing field is written back PRESENT AND EMPTY.
            # Node results are merged into state, so a key that is merely
            # omitted keeps whatever it held — omitting `result` here would
            # leave the refused value in place for the projection to find.
            return {
                "formatted_output": WITHHELD_NOTICE,
                "result": None,
                "classification_result": None,
                "status": AgentStatus.ERROR,
                "error_log": list(state.get("error_log") or []) + [f"PostProcessNode: {_WITHHELD_REASON}"],
            }

        emit_trace_event(
            "post_process_complete",
            {
                "routing_decision": result.get("routing_decision"),
                "out_of_scope": result.get("out_of_scope"),
            },
            state,
        )
        return {
            "formatted_output": result,
            "status": AgentStatus.SUCCESS,
        }
