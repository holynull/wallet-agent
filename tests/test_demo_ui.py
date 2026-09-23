from pathlib import Path

DEMO_HTML = Path(__file__).parents[1] / "demo" / "index.html"


def test_demo_has_request_loading_cancel_and_retry_controls():
    html = DEMO_HTML.read_text(encoding="utf-8")

    assert 'id="requestStatus"' in html
    assert 'id="cancelRequestButton"' in html
    assert 'id="retryButton"' in html
    assert "setRequestBusy" in html
    assert "cancelActiveRequest" in html
    assert "retryLastMessage" in html


def test_demo_retry_reuses_the_last_user_message_without_duplicating_it():
    html = DEMO_HTML.read_text(encoding="utf-8")

    assert "state.lastUserMessage" in html
    assert "retryLastMessage" in html
    assert "sendMessage({ message: state.lastUserMessage, addUserMessage: false })" in html


def test_demo_renders_backend_progress_events_in_the_conversation():
    html = DEMO_HTML.read_text(encoding="utf-8")

    assert "event === 'progress'" in html
    assert "progress.message || progress.stage" in html
    assert "progress.elapsed_ms" in html


def test_demo_renders_okx_source_and_history_kind_metadata():
    html = DEMO_HTML.read_text(encoding="utf-8")

    assert "response.source.toUpperCase()" in html
    assert "response.history_kind" in html
    assert "portfolio.source" in html
