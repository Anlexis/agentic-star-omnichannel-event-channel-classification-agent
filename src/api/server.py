"""AgentCore Platform v1.0"""

# Standalone HTTP entry point.
#
# Adapter only — no domain logic here. On the hosted platform the gateway calls
# agent.invoke() directly and this module is not in the path; everything below
# exists so that the standalone deployment behaves the same way the hosted one
# does, rather than nearly the same way.

import os
import re
import secrets
from typing import Any, Dict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.security.credential_detector import detect_credentials
from shared.secrets import factory as secrets_factory
from shared.utils.audit_logger import emit_trace_event

from src.graph.graph import Graph
from src.services.runtime_config import runtime_config

app = FastAPI(title="Agent")

# The registry builds the graph with config/config.yaml already loaded and
# passed in; this server loads the same file and passes it the same way. That
# is what keeps a declared threshold live in both deployments instead of only
# under the registry — a bare Graph() here would leave every declared value
# replaced by a node default, with nothing failing to say so.
agent = Graph(config=runtime_config())
agent.compile()

# The secret provider is scoped by the identity the manifest declares, so a
# secret resolves from the same place here and under the registry. The provider
# reads `env/namespaces/{namespace}/…` and `env/agents/{namespace}/{name}/…`
# (shared/secrets/dotenv_provider.py), so a namespace that disagrees with
# `config/agent.yaml` silently splits one agent's secrets across two stores.
# `namespace` is lower(industry) — "ret" — not the lowercased template id.
# tests/integration/test_manifest_identity_alignment.py holds these two values
# to the manifest; it reads both sides rather than restating them.
agent.provision_secrets(secrets_factory(namespace="ret", agent_name="OmnichannelDataIntegrationAgent"))

# Coarse bound on the request body. The pipeline enforces its own limits on
# field count and payload size; this is the one that keeps an oversized body
# from being decoded at all.
_MAX_INPUT_BYTES = 262_144

# ── Credential screen on the event payload ───────────────────────────────────
# Why this runs before invoke() rather than inside a node:
#
# The framework's mandatory output gate scans every value of every node result
# for credential patterns, and the request text is carried in node results on
# its way through the backbone. A credential-shaped string inside the event
# payload therefore fails a node before this template's own checks run, and
# what the caller gets back is an error status with no field named and no
# reason given.
#
# The request cannot succeed either way — a payload carrying a live key is not
# something this agent should classify. Refusing it here changes nothing about
# what is accepted; it changes an unexplained failure into one the caller can
# act on.
#
# The screen calls the SAME detector the framework's gate calls, so what this
# refuses and what that blocks are one set by construction rather than two
# lists that drift apart.
_SAFE_SNIPPET_RE = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")


class InvokeRequest(BaseModel):
    # The retail event, as a JSON object encoded in a string. The domain parser
    # decodes it; `deploy/invoke_payload.json` and the contract test in
    # tests/integration are built from the same fixture, so the deployed smoke
    # payload and the tests assert the same shape.
    input: str
    session_id: str = ""


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)

    # Standalone caller auth. When INVOKE_AUTH_TOKEN is set on the server
    # environment, a caller that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token, and then runs at
    # VERIFIED_EXTERNAL. Trust established by middleware is never demoted here.
    #
    # Required rather than optional: every node of this agent declares
    # VERIFIED_EXTERNAL — the level the manifest declares — and in a standalone
    # deployment nothing else sets request.state.trust_level. Without this
    # boundary every request arrives ANONYMOUS, the trust gate denies it at the
    # first domain node, and the agent answers an error to every call.
    #
    # This is an entry-point caller credential, not an agent secret: it is read
    # from the environment because no invocation context exists yet to resolve
    # a secret against.
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compared as bytes: compare_digest raises TypeError on non-ASCII str
        # (headers decode as latin-1), which would answer 500 instead of 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — it must not reveal whether the token
            # was absent, malformed, or simply wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    if len(req.input.encode("utf-8")) > _MAX_INPUT_BYTES:
        raise HTTPException(status_code=413, detail="Request body exceeds the maximum allowed size.")

    findings = detect_credentials(req.input)
    if findings:
        kind = str(findings[0].get("type", "credential"))
        emit_trace_event(
            "input_credential_refused",
            {"kind": kind if _SAFE_SNIPPET_RE.match(kind) else "credential"},
            {"session_id": req.session_id},
        )
        # 400, not 422: pydantic owns 422 and answers there with a list of
        # error objects, so reusing it would make client handling ambiguous.
        # The KIND is named; the matched value never is.
        raise HTTPException(
            status_code=400,
            detail=(
                "The event payload contains a credential-shaped value "
                f"({kind}). Remove API keys, tokens and connection strings and retry."
            ),
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "OmnichannelDataIntegrationAgent"}
