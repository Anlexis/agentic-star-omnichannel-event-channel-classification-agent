"""AgentCore Platform v1.0"""

# RET-C2-252 — the domain workflow, as its own graph.
#
#   START -> event_parse_channel_identify -> event_type_classify
#         -> data_quality_score -> keihin_compliance_check -> appi_consent_check
#         -> routing_decide -> output_validate -> END
#
# Linear by design. Each step either produces its finding or short-circuits on
# the out-of-scope flag; there is no branch, so there is no path that can skip
# the compliance checks or the output boundary at the end.
#
# get_output() below and DomainWorkflowGraphNode.merge_output() in graph.py are
# the two halves of one contract and are written to be read together.

from typing import Any, Dict, Mapping, Optional

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.nodes.appi_consent_check import APPIConsentCheckNode
from src.nodes.data_quality_score import DataQualityScoreNode
from src.nodes.event_parse_channel_identify import EventParseChannelIdentifyNode
from src.nodes.event_type_classify import EventTypeClassifyNode
from src.nodes.keihin_compliance_check import KeihinComplianceCheckNode
from src.nodes.output_validate import OutputValidateNode
from src.nodes.routing_decide import RoutingDecideNode
from src.schemas.state import State
from src.services.runtime_config import domain_config


class DomainWorkflowGraph(BaseGraph):
    """The domain pipeline for RET-C2-252."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(dict(config or {}))

    # -- Identity --------------------------------------------------------------

    @property
    def name(self) -> str:
        return "ret_c2_252_domain_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # -- Config validation -----------------------------------------------------

    def _validate_config(self) -> None:
        """Validate the declared thresholds before the graph is built.

        The nodes each validate their own slice in their constructors, which
        run inside register_nodes() a moment from now — so this method exists
        to make that ordering explicit rather than to duplicate the checks. A
        threshold that is missing, non-numeric, non-finite or out of range
        stops the agent from compiling instead of being replaced by a default
        nobody chose.
        """
        domain_config(self.config)

    # -- Node registration -----------------------------------------------------

    def register_nodes(self) -> None:
        """Register the seven domain nodes, each with its declared config.

        No super() call — the base method is abstract. initialize and finalize
        belong to the outer backbone and are not registered here.
        """
        domain = domain_config(self.config)

        self._nodes["event_parse_channel_identify"] = EventParseChannelIdentifyNode()
        self._nodes["event_type_classify"] = EventTypeClassifyNode()
        self._nodes["data_quality_score"] = DataQualityScoreNode(domain)
        self._nodes["keihin_compliance_check"] = KeihinComplianceCheckNode(domain)
        self._nodes["appi_consent_check"] = APPIConsentCheckNode(domain)
        self._nodes["routing_decide"] = RoutingDecideNode(domain)
        self._nodes["output_validate"] = OutputValidateNode()

    # -- Edge wiring -----------------------------------------------------------

    def add_edges(self) -> None:
        """Wire the pipeline. Every registered node appears here exactly once."""
        self._sg.add_edge(START, "event_parse_channel_identify")
        self._sg.add_edge("event_parse_channel_identify", "event_type_classify")
        self._sg.add_edge("event_type_classify", "data_quality_score")
        self._sg.add_edge("data_quality_score", "keihin_compliance_check")
        self._sg.add_edge("keihin_compliance_check", "appi_consent_check")
        self._sg.add_edge("appi_consent_check", "routing_decide")
        self._sg.add_edge("routing_decide", "output_validate")
        self._sg.add_edge("output_validate", END)

    # -- Routing ---------------------------------------------------------------

    def route(self, state: AgentState) -> str:
        """Required by the base class; this topology has no conditional edge.

        add_conditional_edges() is not used, so nothing calls this at runtime.
        It returns END on an error state so that an unexpected call cannot
        re-enter a processing node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_validate"

    # -- Output shape ----------------------------------------------------------

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the dict handed back to the outer graph as `sub_result`.

        Read by DomainWorkflowGraphNode.merge_output() in graph.py.
        """
        return {
            "classification_result": state.get("classification_result"),
            "result": state.get("result", state.get("classification_result")),
            "status": state.get("status"),
            "out_of_scope": state.get("out_of_scope", False),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id"),
            "node_history": state.get("node_history", []),
        }

    def get_state_class(self) -> type:
        """The State TypedDict shared by the inner and outer graphs."""
        return State
