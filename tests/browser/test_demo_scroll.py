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
