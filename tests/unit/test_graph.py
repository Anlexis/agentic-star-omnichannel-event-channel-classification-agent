# RET-C2-252 — Unit Tests: outer graph (OmnichannelClassificationAgent) +
#                          inner graph (DomainWorkflowGraph) wiring
#
# Cat 2 two-layer nested architecture:
#   - OmnichannelClassificationAgent (outer AgentBaseGraph) registers
#     DomainWorkflowGraphNode in the "main" slot and does NOT override add_edges().
#   - DomainWorkflowGraph (inner BaseGraph) registers all 7 domain nodes in
#     pipeline order: event_parse_channel_identify -> event_type_classify ->
#     data_quality_score -> keihin_compliance_check -> appi_consent_check ->
#     routing_decide -> output_validate.
#   - DomainWorkflowGraphNode.merge_output() maps the inner sub_result keys
#     (classification_result / result / status / out_of_scope / error_log) back
#     into the outer state delta (changed keys only).
#   - The outer agent's S-3 hook _extra_security_gate_output delegates to the
#     same run_security_gate() the OutputValidateNode runs.
#
# Asserted against the MERGED develop implementation (911c1ead). Framework-
# dependent instantiation is wrapped in ImportError skips (the SDK wheel may be
# absent in a bare local checkout). The merge_output / get_output / gate-hook
# checks are pure dict ops and run unconditionally once the class imports.

import pytest


class TestInnerDomainWorkflowGraph:
    """Inner BaseGraph: identity, node registration, output shaping."""

    def test_inner_graph_instantiates(self):
        """DomainWorkflowGraph() must not raise NotImplementedError (ABCs filled)."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph

            graph = DomainWorkflowGraph()
            assert graph is not None
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc
        except NotImplementedError as exc:  # pragma: no cover
            pytest.fail(f"DomainWorkflowGraph has unimplemented ABC methods: {exc}")

    def test_inner_graph_identity(self):
        """Inner graph name + state_schema are the shared workflow id and State."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
            from src.schemas.state import State
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        graph = DomainWorkflowGraph()
        assert graph.name == "ret_c2_252_domain_workflow"
        assert graph.state_schema is State

    def test_register_nodes_all_seven_in_pipeline_order(self):
        """All 7 domain nodes register in the linear pipeline order."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
            from src.nodes.event_parse_channel_identify import EventParseChannelIdentifyNode
            from src.nodes.event_type_classify import EventTypeClassifyNode
            from src.nodes.data_quality_score import DataQualityScoreNode
            from src.nodes.keihin_compliance_check import KeihinComplianceCheckNode
            from src.nodes.appi_consent_check import APPIConsentCheckNode
            from src.nodes.routing_decide import RoutingDecideNode
            from src.nodes.output_validate import OutputValidateNode
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        graph = DomainWorkflowGraph()
        # _nodes is populated by register_nodes(); call it directly to avoid
        # depending on a full compile().
        graph._nodes = {}
        graph.register_nodes()

        keys = list(graph._nodes.keys())
        assert keys == [
            "event_parse_channel_identify",
            "event_type_classify",
            "data_quality_score",
            "keihin_compliance_check",
            "appi_consent_check",
            "routing_decide",
            "output_validate",
        ], f"Domain nodes must register in pipeline order, got {keys}"
        assert isinstance(graph._nodes["event_parse_channel_identify"], EventParseChannelIdentifyNode)
        assert isinstance(graph._nodes["event_type_classify"], EventTypeClassifyNode)
        assert isinstance(graph._nodes["data_quality_score"], DataQualityScoreNode)
        assert isinstance(graph._nodes["keihin_compliance_check"], KeihinComplianceCheckNode)
        assert isinstance(graph._nodes["appi_consent_check"], APPIConsentCheckNode)
        assert isinstance(graph._nodes["routing_decide"], RoutingDecideNode)
        assert isinstance(graph._nodes["output_validate"], OutputValidateNode)

    def test_get_output_surfaces_classification_result(self):
        """get_output() emits classification_result + status + out_of_scope + error_log."""
        try:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        graph = DomainWorkflowGraph()
        state = {
            "classification_result": {"channel_id": "ec"},
            "result": {"channel_id": "ec"},
            "status": "SUCCESS",
            "out_of_scope": False,
            "error_log": [],
            "trace_id": "T-1",
            "node_history": ["event_parse_channel_identify"],
        }
        out = graph.get_output(state)
        assert out["classification_result"] == {"channel_id": "ec"}
        assert "status" in out
        assert out["out_of_scope"] is False
        assert out["error_log"] == []


class TestOuterGraphWiring:
    """Outer OmnichannelClassificationAgent + DomainWorkflowGraphNode mapping methods."""

    def test_outer_agent_registers_main_graph_node(self):
        """register_nodes() puts DomainWorkflowGraphNode in the 'main' slot."""
        try:
            from src.graph.graph import (
                OmnichannelClassificationAgent,
                DomainWorkflowGraphNode,
            )
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        agent = OmnichannelClassificationAgent()
        agent._nodes = {}
        agent.register_nodes()

        assert "main" in agent._nodes
        assert isinstance(agent._nodes["main"], DomainWorkflowGraphNode)

    def test_outer_graph_does_not_override_add_edges(self):
        """The outer backbone wiring belongs to the framework — add_edges not overridden."""
        try:
            from src.graph.graph import OmnichannelClassificationAgent
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        # add_edges must NOT be defined directly on the outer subclass.
        assert "add_edges" not in OmnichannelClassificationAgent.__dict__

    def test_outer_agent_identity(self):
        """OmnichannelClassificationAgent name + state_schema + Graph alias are correct."""
        try:
            from src.graph.graph import OmnichannelClassificationAgent, Graph
            from src.schemas.state import State
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        agent = OmnichannelClassificationAgent()
        assert agent.name == "RET-C2-252"
        assert agent.state_schema is State
        assert Graph is OmnichannelClassificationAgent

    def test_merge_output_maps_inner_keys(self):
        """merge_output maps the inner sub_result keys into the outer delta."""
        try:
            from src.graph.graph import DomainWorkflowGraphNode
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        node = DomainWorkflowGraphNode()
        sub_result = {
            "classification_result": {"channel_id": "ec"},
            "result": {"channel_id": "ec"},
            "status": "SUCCESS",
            "out_of_scope": False,
            "error_log": [],
        }
        merged = node.merge_output({}, sub_result)
        assert merged["classification_result"] == {"channel_id": "ec"}
        assert merged["status"] == "SUCCESS"
        assert merged["out_of_scope"] is False

    def test_merge_output_returns_only_changed_keys(self):
        """merge_output must NOT echo the outer state back."""
        try:
            from src.graph.graph import DomainWorkflowGraphNode
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        node = DomainWorkflowGraphNode()
        outer_state = {"user_input": "raw", "session_id": "S-1"}
        sub_result = {
            "classification_result": {},
            "result": {},
            "status": "SUCCESS",
            "out_of_scope": False,
            "error_log": [],
        }

        merged = node.merge_output(outer_state, sub_result)
        assert "user_input" not in merged
        assert "session_id" not in merged
        assert set(merged.keys()) == {
            "classification_result",
            "result",
            "status",
            "out_of_scope",
            "error_log",
        }

    def test_get_subgraph_returns_inner_graph(self):
        """get_subgraph() returns a DomainWorkflowGraph instance."""
        try:
            from src.graph.graph import DomainWorkflowGraphNode
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        node = DomainWorkflowGraphNode()
        assert isinstance(node.get_subgraph(), DomainWorkflowGraph)

    def test_extra_security_gate_hook_delegates_to_run_security_gate(self):
        """The outer S-3 hook _extra_security_gate_output uses the shared gate."""
        try:
            from src.graph.graph import OmnichannelClassificationAgent
        except ImportError as exc:
            raise AssertionError(f"the module under test did not import: {exc}") from exc

        agent = OmnichannelClassificationAgent()
        clean_result = {
            "channel_id": "ec",
            "channel_confidence": 1.0,
            "event_type": "purchase",
            "event_type_confidence": 1.0,
            "data_quality_score": 0.95,
            "data_quality_flags": [],
            "keihin_compliant": True,
            "keihin_violations": [],
            "appi_consent_status": "granted",
            "appi_flags": [],
            "routing_decision": "realtime",
            "routing_priority": 1,
            "out_of_scope": False,
        }
        ok, _reason = agent._extra_security_gate_output({"classification_result": clean_result})
        assert ok is True

        bad_ok, bad_reason = agent._extra_security_gate_output({"classification_result": {}})
        assert bad_ok is False
        assert bad_reason
