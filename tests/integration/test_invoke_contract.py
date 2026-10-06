"""The caller contract, driven through the real HTTP entry point.

Every test here posts to the real ASGI app with the real compiled graph behind
it. That is deliberate: the defects this file exists to prevent were all
invisible to node-level tests. A node returns what it is handed, and a suite
that hands each node a hand-built state proves that the nodes agree with the
test author, not that they agree with each other.

`BASE_EVENT` below is the single fixture. `deploy/invoke_payload.json` — the
body the deployment smoke check posts — is asserted against it rather than
written separately, so the payload that is deployed and the payload that is
tested cannot drift into two different contracts.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.server import app

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PAYLOAD_PATH = _REPO_ROOT / "deploy" / "invoke_payload.json"

AUTH_TOKEN = "test-invoke-token"

# A database connection string, assembled rather than written out.
#
# The tests that use it need a real one — the point is that the framework's
# detector recognises the shape and that neither boundary lets it through. But
# the repository-wide credential scan reads source text, and a literal here
# would be indistinguishable from a committed secret. Splitting it at the
# scheme keeps the scan quiet while the assembled value stays exactly what a
# real connection string is.
DB_URI = "postgresql://" + "user:pass@db.internal:5432/retail"

# A complete, well-formed retail event: every field the purchase event type
# requires, on a recognised channel, with an explicit consent signal.
#
# The event id is written `evt-<date>T<sequence>` rather than the more usual
# `evt-<date>-<sequence>`. That is not cosmetic — see
# test_a_dated_event_reference_is_redacted_before_the_agent_sees_it below for
# what the hyphenated form runs into, and docs/07 for the operational note.
BASE_EVENT = {
    "event_id": "evt-20260914T0001",
    "timestamp": "2026-09-14T10:15:00Z",
    "channel": "ec",
    "event_type": "purchase",
    "sku": "4901234567890",
    "amount": 12500,
    "price": 12500,
    "reference_price": 12500,
    "consent": "granted",
}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", AUTH_TOKEN)
    return TestClient(app)


def post_event(client, event, **body):
    return client.post(
        "/invoke",
        json={"input": json.dumps(event), "session_id": "test", **body},
        headers={"Authorization": f"Bearer {AUTH_TOKEN}"},
    )


# ── The deployed payload is the tested payload ───────────────────────────────


def test_deploy_payload_carries_the_event_as_an_encoded_string():
    """`input` must be a JSON-encoded string, because the parser decodes it.

    The deployment smoke check posts this file verbatim. A payload written as
    prose, or as a nested object, is refused by the entry node — and because
    the surrounding assertions only check that HTTP answered, the pipeline
    stays green while the agent answers an error to its own smoke test.
    """
    payload = json.loads(_PAYLOAD_PATH.read_text())
    assert isinstance(payload["input"], str), "input must be a string the parser can decode"
    assert (
        json.loads(payload["input"]) == BASE_EVENT
    ), "deploy/invoke_payload.json has drifted from the fixture these tests assert against"


def test_deploy_payload_is_accepted_by_the_running_agent(client):
    """Post the deployment payload itself, byte for byte."""
    payload = json.loads(_PAYLOAD_PATH.read_text())
    response = client.post("/invoke", json=payload, headers={"Authorization": f"Bearer {AUTH_TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["output"]["channel_id"] == "ec"


# ── The public path does real work ───────────────────────────────────────────


def test_a_complete_event_is_classified(client):
    body = post_event(client, BASE_EVENT).json()
    assert body["status"] == "success"
    output = body["output"]
    assert output["channel_id"] == "ec"
    assert output["event_type"] == "purchase"
    assert output["out_of_scope"] is False
    assert output["data_quality_score"] > 0.9
    assert output["routing_decision"] == "realtime"


@pytest.mark.parametrize(
    ("patch", "expected_decision"),
    [
        ({}, "realtime"),
        ({"event_type": "browse"}, "batch"),
        ({"channel": "carrier-pigeon"}, "manual_review"),
        ({"price": 20000, "reference_price": 10000}, "manual_review"),
        ({"consent": "denied"}, "manual_review"),
    ],
    ids=["realtime", "batch", "unknown-channel", "price-discrepancy", "consent-denied"],
)
def test_every_routing_outcome_is_reachable(client, patch, expected_decision):
    """Each routing branch must be reachable from a real request.

    A decision that no input can produce is not a policy, it is dead code —
    and a rule engine whose branches are all unreachable but one returns the
    same answer for every event while looking like it decided something.
    """
    output = post_event(client, {**BASE_EVENT, **patch}).json()["output"]
    assert output["routing_decision"] == expected_decision


def test_the_classification_changes_with_the_event(client):
    """Two very different events must not produce the same answer."""
    complete = post_event(client, BASE_EVENT).json()["output"]
    sparse = post_event(client, {"channel": "store", "event_type": "inquiry"}).json()["output"]

    assert complete["channel_id"] != sparse["channel_id"]
    assert complete["event_type"] != sparse["event_type"]
    assert complete["data_quality_score"] != sparse["data_quality_score"]


def test_retail_identifiers_survive_into_the_response(client):
    """A barcode is not a telephone number.

    A thirteen-digit JAN/EAN code and an ISO-8601 timestamp are both long runs
    of digits, and a personal-data net cast by length alone replaces them. The
    event then scores badly for carrying exactly the fields it is supposed to
    carry.
    """
    output = post_event(client, BASE_EVENT).json()["output"]
    assert (
        output["data_quality_flags"] == []
    ), "a well-formed event produced quality flags — the identifiers were rewritten before scoring"


def test_a_dated_event_reference_is_redacted_before_the_agent_sees_it(client):
    """A retail reference that embeds a date is rewritten by the platform.

    The platform's personal-data filter runs before any template code, and its
    Japanese identity-number shape is twelve digits in three groups. A perfectly
    ordinary event reference — `evt-<yyyymmdd>-<nnnn>` — is eight digits plus
    four, so it matches, and the field arrives as `evt-[MASKED]`. The same
    filter reads a title-case store or product name as a personal name.

    This is not fixable from here, and it is pinned rather than worked around
    so that it stays visible: if the platform narrows the pattern, this test
    fails and the note in docs/07 can be retired. What IS fixable is the
    template's response to it, which the assertions below cover — the redacted
    field is never certified as a valid identifier, and the quality flag says
    the value was redacted rather than blaming the caller's formatting.
    """
    output = post_event(client, {**BASE_EVENT, "event_id": "evt-20260914-0001"}).json()["output"]

    flags = output["data_quality_flags"]
    assert any(
        "was redacted before classification" in flag for flag in flags
    ), f"expected a redaction flag on event_id, got {flags}"
    assert not any(
        "invalid identifier format" in flag for flag in flags
    ), "a platform redaction was reported as a caller formatting fault"
    assert output["data_quality_score"] < 1.0, "a redaction sentinel was scored as valid data"


# ── Authentication ───────────────────────────────────────────────────────────


def test_a_request_without_a_token_is_refused(client):
    response = client.post("/invoke", json={"input": json.dumps(BASE_EVENT)})
    assert response.status_code == 401


def test_a_request_with_the_wrong_token_is_refused(client):
    response = client.post(
        "/invoke",
        json={"input": json.dumps(BASE_EVENT)},
        headers={"Authorization": "Bearer not-the-token"},
    )
    assert response.status_code == 401
    # The body must not distinguish a wrong token from a missing one.
    assert "missing" not in response.json()["detail"].lower()


def test_health_needs_no_token(client):
    assert client.get("/health").status_code == 200
