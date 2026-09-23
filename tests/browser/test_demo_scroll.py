import pytest
from playwright.sync_api import Page


def scroll_metrics(page: Page) -> dict[str, int]:
    return page.locator("#messages").evaluate(
        """node => ({
            scrollTop: Math.round(node.scrollTop),
            clientHeight: Math.round(node.clientHeight),
            scrollHeight: Math.round(node.scrollHeight),
        })"""
    )


@pytest.mark.browser
def test_demo_follows_output_pauses_for_history_and_resumes_at_bottom(demo_page: Page):
    demo_page.evaluate(
        """() => {
            for (let index = 0; index < 60; index += 1) {
                addMessage('assistant', `历史消息 ${index}：${'内容'.repeat(20)}`);
            }
        }"""
    )
    demo_page.wait_for_timeout(100)
    at_bottom = scroll_metrics(demo_page)
    assert at_bottom["scrollHeight"] > at_bottom["clientHeight"]
    assert at_bottom["scrollHeight"] - at_bottom["clientHeight"] - at_bottom["scrollTop"] <= 2

    demo_page.locator("#messages").evaluate(
        """node => {
            node.scrollTop = 0;
            node.dispatchEvent(new Event('scroll'));
        }"""
    )
    demo_page.wait_for_timeout(50)
    demo_page.evaluate("addMessage('assistant', '用户查看历史时的新输出')")
    demo_page.wait_for_timeout(100)
    paused = scroll_metrics(demo_page)
    assert paused["scrollTop"] <= 2

    demo_page.locator("#messages").evaluate(
        """node => {
            node.scrollTop = node.scrollHeight;
            node.dispatchEvent(new Event('scroll'));
        }"""
    )
    demo_page.wait_for_timeout(50)
    demo_page.evaluate("addMessage('assistant', '恢复跟随后的一条新输出')")
    demo_page.wait_for_timeout(100)
    resumed = scroll_metrics(demo_page)
    assert resumed["scrollHeight"] - resumed["clientHeight"] - resumed["scrollTop"] <= 2


@pytest.mark.browser
def test_demo_renders_identical_assistant_responses_from_different_runs(demo_page: Page):
    demo_page.evaluate(
        """() => {
            const response = {
                kind: 'swap_status',
                status: 'pending',
                message: '相同的状态回复',
            };
            state.runId = 'newest-run';
            applyEvent('update', { response }, true, 'run-one');
            applyEvent('update', { response }, true, 'run-two');
            applyEvent('complete', { state: { response } }, true, 'run-one');
            applyEvent('complete', { state: { response } }, true, 'run-three');
        }"""
    )
    demo_page.wait_for_timeout(500)

    replies = demo_page.locator(".message.assistant .bubble", has_text="相同的状态回复")
    assert replies.count() == 3


@pytest.mark.browser
def test_demo_copies_the_complete_debug_output(demo_page: Page):
    demo_page.evaluate(
        """() => {
            Object.defineProperty(navigator, 'clipboard', {
                configurable: true,
                value: { writeText: async (text) => { window.copiedDebugText = text; } },
            });
            recordDebug('COPY TEST', { answer: 42 });
        }"""
    )

    expected = demo_page.locator("#debugOutput").text_content()
    demo_page.locator("#debugPanel").evaluate("node => { node.open = true; }")
    demo_page.locator("#debugCopyButton").click()

    assert demo_page.locator("#debugCopyButton").text_content() == "已复制"
    assert demo_page.evaluate("window.copiedDebugText") == expected


@pytest.mark.browser
def test_demo_reports_clipboard_failures(demo_page: Page):
    demo_page.evaluate(
        """() => {
            Object.defineProperty(navigator, 'clipboard', {
                configurable: true,
                value: { writeText: async () => { throw new Error('denied'); } },
            });
            recordDebug('COPY TEST', { answer: 42 });
        }"""
    )

    demo_page.locator("#debugPanel").evaluate("node => { node.open = true; }")
    demo_page.locator("#debugCopyButton").click()

    assert demo_page.locator("#debugCopyButton").text_content() == "复制失败"
