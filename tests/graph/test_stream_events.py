"""Custom stream events: the card payload must reach a platform-hosted client."""

from wallet_agent.graph.emit import CUSTOM_EVENT, build_custom_payload, emit_response


def test_payload_wraps_the_finished_response():
    payload = build_custom_payload({"kind": "react", "message": "ok"})

    assert payload["kind"] == CUSTOM_EVENT
    assert payload["response"] == {"kind": "react", "message": "ok"}


def test_emission_writes_once_and_reports_success():
    written = []

    assert emit_response({"kind": "react"}, writer=written.append) is True
    assert written == [{"kind": CUSTOM_EVENT, "response": {"kind": "react"}}]


def test_emission_is_best_effort():
    # Nothing to send, no writer, and a writer that explodes: never raise.
    assert emit_response(None, writer=lambda _: None) is False
    assert emit_response({}, writer=lambda _: None) is False
    assert emit_response({"kind": "react"}, writer=lambda _: False) is True

    def boom(_payload):
        raise RuntimeError("stream closed")

    assert emit_response({"kind": "react"}, writer=boom) is False
