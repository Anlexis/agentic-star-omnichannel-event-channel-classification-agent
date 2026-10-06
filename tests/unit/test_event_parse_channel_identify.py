# RET-C2-252 — Unit Tests: EventParseChannelIdentifyNode
#
# Inner DomainWorkflowGraph node 1: parse the raw event payload from raw_input,
# mask customer PII (S-4) BEFORE writing any state key, then identify and
# canonicalise the channel with a rule-based confidence.
#
# Asserted against the MERGED develop implementation (911c1ead):
#   - A valid payload with a recognised channel -> channel_id + channel_confidence
#     set, out_of_scope=False, status=SUCCESS.
#   - Customer email/phone in the payload are MASKED in parsed_payload (the raw
#     values never survive into state) and never reach the S-4 audit PAYLOAD.
#   - Channel scope (per the merged _identify_channel rules):
#       * A present channel token that fuzzy-matches a synonym substring is
#         mapped to that canonical channel -> out_of_scope=False (e.g.
#         "carrier_pigeon_xyz" contains the "x" synonym -> "sns", conf 0.7).
#       * A present channel token that matches NO synonym at all -> "other",
#         out_of_scope=True (e.g. "telegraph").
#       * NO channel field at all -> "other", out_of_scope=True.
#     In every case the status is SUCCESS (out-of-scope is a graceful success,
#     not an error).
#   - Absent / empty / non-str-non-dict raw_input -> status=ERROR (S-1).
#
# S-4 audit: emit_trace_event is muted at the node module via an autouse fixture
# (NEVER stub shared.* in sys.modules — the CI wheel ships a real shared package).

import json
from unittest.mock import MagicMock

import pytest

from src.nodes.event_parse_channel_identify import EventParseChannelIdentifyNode
from framework.schemas.agent_status import AgentStatus


@pytest.fixture(autouse=True)
def _mute_audit(monkeypatch):
    """Silence the S-4 audit free function at the node module under test."""
    monkeypatch.setattr(
        "src.nodes.event_parse_channel_identify.emit_trace_event",
        lambda *a, **k: None,
    )


def _payload(**overrides):
    """A complete, in-scope event payload with a recognised channel."""
    base = {
        "channel": "ec",
        "event_type": "purchase",
        "event_id": "EVT-001",
        "timestamp": "2026-06-25T10:00:00Z",
        "sku": "SKU-123",
        "amount": 4980,
    }
    base.update(overrides)
    return base


class TestEventParseChannelIdentifyNode:
    """Unit tests for EventParseChannelIdentifyNode (parse + PII mask + channel)."""

    def setup_method(self):
        self.node = EventParseChannelIdentifyNode()

    def test_happy_path_identifies_channel(self):
        """Valid payload -> canonical channel_id, confidence, out_of_scope=False."""
        result = self.node.execute({"raw_input": json.dumps(_payload())})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["channel_id"] == "ec"
        assert result["channel_confidence"] == 1.0
        assert result["out_of_scope"] is False
        assert result["event_type_raw"] == "purchase"
        assert isinstance(result["parsed_payload"], dict)

    def test_dict_raw_input_accepted(self):
        """raw_input may be a dict directly (not only a JSON string)."""
        result = self.node.execute({"raw_input": _payload(channel="store")})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["channel_id"] == "store"
        assert result["out_of_scope"] is False

    def test_channel_synonym_maps_to_canonical(self):
        """A synonym token (e.g. 'webstore') maps to the canonical channel."""
        result = self.node.execute({"raw_input": json.dumps(_payload(channel="webstore"))})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["channel_id"] == "ec"
        assert result["out_of_scope"] is False

    def test_pii_masked_in_parsed_payload(self):
        """Customer email/phone are masked in parsed_payload (raw never persisted)."""
        payload = _payload(
            email="taro.yamada@example.com",
            phone="090-1234-5678",
            customer_name="山田太郎",
        )
        result = self.node.execute({"raw_input": json.dumps(payload)})

        assert result["status"] == AgentStatus.SUCCESS
        parsed_repr = repr(result["parsed_payload"])
        assert "taro.yamada@example.com" not in parsed_repr
        assert "090-1234-5678" not in parsed_repr
        assert "山田太郎" not in parsed_repr

    def test_pii_not_emitted_in_audit_payload(self):
        """S-4: raw customer PII never appears in the emitted audit PAYLOAD (args[1])."""
        spy = MagicMock()
        import src.nodes.event_parse_channel_identify as mod

        original = mod.emit_trace_event
        mod.emit_trace_event = spy
        try:
            payload = _payload(email="taro.yamada@example.com", phone="090-1234-5678")
            result = self.node.execute({"raw_input": json.dumps(payload)})
        finally:
            mod.emit_trace_event = original

        assert result["status"] == AgentStatus.SUCCESS
        assert spy.called
        # The 2nd positional arg is the audit event PAYLOAD the logger persists;
        # the 3rd arg is the working state, which legitimately still carries
        # masked context. Assert against the PAYLOAD only.
        emitted_payloads = repr([c.args[1] for c in spy.call_args_list if len(c.args) > 1])
        assert "taro.yamada@example.com" not in emitted_payloads
        assert "090-1234-5678" not in emitted_payloads

    def test_fuzzy_channel_token_is_in_scope(self):
        """A present token that fuzzy-matches a synonym substring is in scope.

        Per the merged _identify_channel, after exact/synonym lookups miss, the
        token is matched against synonym *substrings*. "carrier_pigeon_xyz"
        contains the "x" synonym (-> 'sns'), so it is treated as a recognised
        channel with confidence 0.7 and out_of_scope=False — NOT 'other'.
        """
        result = self.node.execute({"raw_input": json.dumps(_payload(channel="carrier_pigeon_xyz"))})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is False
        assert result["channel_id"] == "sns"
        assert result["channel_confidence"] == 0.7

    def test_unmappable_channel_token_is_other_out_of_scope_success(self):
        """A present token matching NO synonym at all -> 'other', out_of_scope=True.

        "telegraph" contains none of the channel synonym tokens, so it falls
        through to the present-but-unmappable branch: channel_id='other',
        confidence 0.3, out_of_scope=True, status=SUCCESS.
        """
        result = self.node.execute({"raw_input": json.dumps(_payload(channel="telegraph"))})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True
        assert result["channel_id"] == "other"

    def test_missing_channel_field_is_out_of_scope_success(self):
        """No channel field at all -> out_of_scope=True, status=SUCCESS."""
        payload = _payload()
        del payload["channel"]
        result = self.node.execute({"raw_input": json.dumps(payload)})

        assert result["status"] == AgentStatus.SUCCESS
        assert result["out_of_scope"] is True
        assert result["channel_id"] == "other"

    def test_absent_raw_input_is_error(self):
        """No raw_input and no user_input -> structural ERROR (S-1)."""
        result = self.node.execute({"error_log": []})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]  # non-empty

    def test_empty_string_raw_input_is_error(self):
        """An empty / whitespace raw_input string -> ERROR (S-1)."""
        result = self.node.execute({"raw_input": "   "})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]

    def test_unparseable_raw_input_is_error(self):
        """A non-JSON, non-dict raw_input string -> ERROR (S-1)."""
        result = self.node.execute({"raw_input": "}{not valid json"})

        assert result["status"] == AgentStatus.ERROR
        assert result["error_log"]
