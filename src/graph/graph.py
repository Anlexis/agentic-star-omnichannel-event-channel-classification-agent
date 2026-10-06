"""AgentCore Platform v1.0"""

# RET-C2-252 — outer graph.
#
# Two layers. The outer one is the fixed backbone every agent shares:
#
#   START -> initialize -> pre_process -> main -> post_process -> finalize -> END
#
# and the `main` slot holds a node that delegates the whole domain workflow to
# an inner graph (src/graph/domain_workflow_graph.py):
#
#   parse & identify channel -> classify event type -> score data quality
#     -> pricing compliance -> consent check -> route -> output boundary
#
# Configuration travels down that chain explicitly — entry point to outer graph
# to main-slot node to inner graph to each node's constructor. It has to be
# carried by hand because nothing carries it automatically: the framework calls
# a node as `node(state)`, so a node that reads its thresholds from an `execute`
# argument reads `None` on every real invocation and silently uses its defaults
# for the life of the deployment.

from typing import Any, ClassVar, Dict, Mapping, Optional, Tuple

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.graph.base_graph import BaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState

from src.nodes.output_validate import run_security_gate
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.runtime_config import runtime_config  # noqa: F401 — re-exported for the entry point


class DomainWorkflowGraphNode(GraphNode):
    """The `main` slot — delegates to the inner domain workflow graph."""

    # Re-raise inner failures rather than converting them into a partial
    # success the caller cannot distinguish from a real one.
    error_strategy: ClassVar[str] = "propagate"

    # Human review, where this workflow uses it, happens inside the inner graph.
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, config: Optional[Mapping[str, Any]] = None) -> None:
        self._config: Dict[str, Any] = dict(config or {})

    def get_subgraph(self) -> "BaseGraph":
        """Build the inner graph, carrying the declared configuration into it.

        Imported here rather than at module scope to keep the two graph modules
        from importing each other at load time.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=self._config)

    def extract_input(self, state: AgentState) -> str:
        """Return the event payload as the STRING the inner graph is invoked with.

        The return value of this method is passed straight through as the inner
        graph's `user_input` positional argument. Returning a mapping here does
        not seed inner state with its keys — it makes the whole mapping the
        inner `user_input`, so the domain parser receives `{"raw_input": "..."}`
        and reads a payload with no channel field, no event type and no
        timestamp in it. Every classification then comes back as the
        unrecognised default, for every input, with a successful status.
        """
        payload = state.get("validated_input")
        if payload is None:
            payload = state.get("raw_input")
        if payload is None:
            payload = state.get("user_input", "")
        return payload if isinstance(payload, str) else str(payload)

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map the inner graph's output into the outer state delta.

        Only changed keys. Field names are shared with
        DomainWorkflowGraph.get_output(), which is the other half of this
        contract; tests/unit/test_graph.py holds the two together.
        """
        return {
            "classification_result": sub_result.get("classification_result"),
            "result": sub_result.get("result", sub_result.get("classification_result")),
            "status": sub_result.get("status"),
            "out_of_scope": sub_result.get("out_of_scope", False),
            "error_log": sub_result.get("error_log", []),
        }


class OmnichannelClassificationAgent(AgentBaseGraph):
    """Classifies a retail omnichannel event and decides how it should be routed.

    Inherits the framework base graph directly. All domain logic lives in the
    inner graph reached through the `main` slot; `register_nodes()` is the only
    backbone method overridden, and the edges are the framework's.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with the agent registry."""
        return "RET-C2-252"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill the five backbone slots.

        The base implementation runs first — it supplies the framework's own
        initialize and finalize nodes.
        """
        super().register_nodes()

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = DomainWorkflowGraphNode(self.config)
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is deliberately not overridden — the backbone wiring is the
    # framework's, and this agent adds no branch to it.

    def _validate_config(self) -> None:
        """Validate the backbone config, and the domain config with it.

        The domain thresholds are validated by the inner nodes' constructors,
        and those run inside the inner graph — which is built lazily, on the
        first request. Without this, a malformed `domain:` block would let the
        agent start cleanly and then fail on the first caller's event, in a
        deployment where nothing had gone wrong since the last green start.

        Building the inner graph here runs those same constructors at compile
        time. It is the constructors that are re-used rather than a second copy
        of the bounds, so there is nothing here that can disagree with what the
        nodes actually enforce.
        """
        super()._validate_config()

        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        DomainWorkflowGraph(config=self.config).compile()

    def _extra_security_gate_output(self, state: State) -> Tuple[bool, Optional[str]]:
        """Graph-level output hook — the same boundary the pipeline enforces.

        The framework's own gate method is final and must not be overridden;
        this is the extension point it calls. It delegates to the shared gate so
        that the pipeline's boundary, the backbone's output stage and this hook
        cannot come to three different conclusions about one response.
        """
        return run_security_gate(state.get("classification_result"))


# Back-compat alias — the manifest names the class above; callers may use either.
Graph = OmnichannelClassificationAgent
