import pytest
from playwright.sync_api import Page


@pytest.mark.browser
def test_demo_renders_okx_price_history_candles_and_broadcast_diagnostics(demo_page: Page):
    demo_page.evaluate(
        """() => {
            renderResponse({
              kind: 'price_query',
              prices: [{
                asset: { chain: 'ETH', symbol: 'ETH' },
                usd_price: '3200', provider: 'okx',
                observed_at: '2026-09-21T01:00:00Z'
              }],
              details: [{
                asset: { chain: 'ETH', symbol: 'ETH' },
                usd_price: '3200', provider: 'okx',
                holder_count: 12, market_cap: '100'
              }],
              history: {
                points: [{
                  price: '3100', observed_at: '2026-09-20T01:00:00Z'
                }],
                next_cursor: 'cursor-1'
              },
              candles: [{
                open: '1', high: '2', low: '0.5', close: '1.5',
                observed_at: '2026-09-21T01:00:00Z', provider: 'okx'
              }]
            });
            renderResponse({
              kind: 'swap_status', status: 'not_propagated',
              broadcast_status: 'not_propagated',
              tx_hash: '0x' + '1'.repeat(64),
              message: 'hash not yet visible'
            });
            renderPreflight({
              ok: true,
              gas_sources: { rpc: true, okx: true, simulation: false },
              simulation: {
                success: false, error: 'simulation unavailable'
              },
              checks: [{
                status: 'warning', message: 'simulation unavailable'
              }]
            });
        }"""
    )
    text = demo_page.locator("body").inner_text()
    assert "OKX" in text
    assert "2026-09-21T01:00:00Z" in text
    assert "历史价格" in text
    assert "K 线" in text
    assert "not_propagated" in text
    assert "simulation unavailable" in text


@pytest.mark.browser
def test_demo_debug_data_is_copyable_and_redacts_credentials(demo_page: Page):
    demo_page.context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin=demo_page.url.split("/demo/")[0],
    )
    demo_page.evaluate(
        """() => recordDebug('TEST', {
            headers: { 'OK-ACCESS-KEY': 'secret-key' },
            api_key: 'secret', value: 'visible'
        })"""
    )
    output = demo_page.locator("#debugOutput").text_content() or ""
    assert "visible" in output
    assert "secret-key" not in output
    assert "OK-ACCESS-KEY" not in output
    demo_page.locator("#debugPanel").evaluate("node => { node.open = true; }")
    demo_page.locator("#debugCopyButton").click()
    assert demo_page.locator("#debugCopyButton").inner_text() == "已复制"
    copied = demo_page.evaluate("navigator.clipboard.readText()")
    assert "visible" in copied
    assert "secret-key" not in copied


@pytest.mark.browser
def test_identical_status_response_is_rendered_once_per_agent_run(demo_page: Page):
    demo_page.evaluate(
        """() => {
            const response = {
              kind: 'swap_status', status: 'pending',
              message: '兑换订单仍在处理中。'
            };
            state.runId = 'run-1'; renderResponse(response); renderResponse(response);
            state.runId = 'run-2'; renderResponse(response);
        }"""
    )
    demo_page.wait_for_function("state.typingQueue.then(() => true)")
    assert demo_page.locator(".message.assistant", has_text="兑换订单仍在处理中。").count() == 2
