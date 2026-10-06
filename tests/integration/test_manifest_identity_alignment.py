"""The identity the entry point runs under, against the identity the manifest declares.

The manifest is the source of truth for who this agent is, and two separate
things are scoped by that identity.

The secret provider reads `env/namespaces/{namespace}/.env.{env}` and
`env/agents/{namespace}/{agent_name}/.env.{env}`. Under the registry that scope
comes from `config/agent.yaml`; standalone it comes from whatever
`src/api/server.py` passes to the factory. When the two disagree, one agent's
secrets live in two stores, and a key provisioned for the registry deployment is
simply absent in the standalone one — with no error at boot, because a missing
tier file is ignored by design. Nothing fails until a secret is declared, and
then it fails in only one of the two deployments.

The trust gate is scoped by it too. Every node declares the minimum caller trust
it will run for, and the manifest declares the trust level callers are told to
present. A node above that value denies the callers the manifest invites — not
at the boundary, where it would be obvious, but partway through the pipeline,
as an error with a successful-looking HTTP status.

Both sides are READ here, never restated. A test that spelled the expected
namespace out twice would keep passing through exactly the drift it exists to
catch.
"""

from pathlib import Path

from framework.nodes.function_node import FunctionNode
from framework.schemas.trust_level import TrustLevel
from framework.utils.config_loader import load_config

from src.api.server import agent
from src.graph.domain_workflow_graph import DomainWorkflowGraph

# tests/integration/<this file> -> parents[2] is the repository root.
_MANIFEST_PATH = Path(__file__).resolve().parents[2] / "config" / "agent.yaml"
_MANIFEST = load_config(str(_MANIFEST_PATH))

# The provider the entry point bound at import time — the identity that is live
# in a standalone deployment, not a re-derivation of it. `_namespace` and
# `_agent_name` are declared on the SecretProvider base class, which is also
# where the framework reads them from when it reports a missing secret.
_PROVISIONED = agent._secrets_provider

_TRUST_ORDER = [TrustLevel.ANONYMOUS, TrustLevel.VERIFIED_EXTERNAL, TrustLevel.INTERNAL]


def test_manifest_declares_the_identity_fields() -> None:
    """The fields every comparison below rests on must be present.

    Without this, a manifest that lost `namespace:` would make each assertion
    `None == None` and the file would pass while asserting nothing.
    """
    assert _MANIFEST.get("namespace"), f"{_MANIFEST_PATH} declares no namespace"
    assert _MANIFEST.get("name"), f"{_MANIFEST_PATH} declares no name"
    assert _MANIFEST.get("industry"), f"{_MANIFEST_PATH} declares no industry"
    assert _MANIFEST.get("required_trust_level"), f"{_MANIFEST_PATH} declares no required_trust_level"


def test_provisioned_namespace_matches_the_manifest() -> None:
    assert _PROVISIONED._namespace == _MANIFEST["namespace"], (
        "src/api/server.py provisions secrets under namespace "
        f"{_PROVISIONED._namespace!r}, but config/agent.yaml declares "
        f"{_MANIFEST['namespace']!r}. A secret would resolve from a different "
        "store standalone than under the registry."
    )


def test_provisioned_agent_name_matches_the_manifest() -> None:
    assert _PROVISIONED._agent_name == _MANIFEST["name"], (
        "src/api/server.py provisions secrets for agent name "
        f"{_PROVISIONED._agent_name!r}, but config/agent.yaml declares "
        f"{_MANIFEST['name']!r}."
    )


def test_manifest_namespace_is_lower_industry() -> None:
    """`namespace:` is `lower(industry)` — the fleet-wide convention.

    Checked against the manifest's own `industry` field, so this pins which of
    the two values is the correct one to align on without hard-coding either.
    Re-aligning the wrong way — moving the manifest onto the entry point's
    value instead of the reverse — fails here rather than passing as a fix.
    """
    assert _MANIFEST["namespace"] == _MANIFEST["industry"].lower(), (
        f"config/agent.yaml declares namespace {_MANIFEST['namespace']!r}; the "
        f"convention is lower(industry) = {_MANIFEST['industry'].lower()!r}."
    )


def _every_node():
    """Every node the compiled agent will actually run, outer and inner."""
    inner = DomainWorkflowGraph()
    inner.compile()
    yield from agent._nodes.items()
    yield from inner._nodes.items()


def test_no_node_demands_more_trust_than_the_manifest_declares() -> None:
    """A node above the declared level denies the callers the manifest invites.

    Read from the compiled graphs rather than from a list of class names, so a
    node added later is covered without anyone remembering to add it here.
    """
    declared = TrustLevel(_MANIFEST["required_trust_level"])
    ceiling = _TRUST_ORDER.index(declared)

    too_strict = {
        name: node.__class__.required_trust_level.value
        for name, node in _every_node()
        if _TRUST_ORDER.index(node.__class__.required_trust_level) > ceiling
    }
    assert not too_strict, (
        f"config/agent.yaml declares required_trust_level {declared.value!r}, but these nodes "
        f"demand more: {too_strict}. A caller presenting the declared level is denied partway "
        "through the pipeline."
    )


def test_every_node_declares_its_trust_level_explicitly() -> None:
    """An inherited default is not a declaration.

    The framework refuses to define a subclass that leaves this implicit, so a
    missing declaration is an import-time failure rather than a quiet default —
    which means it takes the whole entry point down with it. This test names the
    node instead of leaving a bare TypeError to be traced back.
    """
    undeclared = [
        name
        for name, node in _every_node()
        if isinstance(node, FunctionNode) and "required_trust_level" not in type(node).__dict__
    ]
    assert not undeclared, f"nodes with no explicit required_trust_level: {undeclared}"
