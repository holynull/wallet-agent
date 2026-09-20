# Wallet App Agent Evaluation Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix native-asset swap resolution and Demo scrolling, then add deterministic multi-turn and real-browser regression coverage that produces a machine-readable evaluation report.

**Architecture:** Keep asset resolution inside the existing LangGraph boundary, but distinguish native assets from contract tokens before provider catalog lookup. Extend the existing deterministic evaluation runner for the failed three-turn conversation, and add a Python Playwright layer that drives the served Demo in a real Chromium viewport so DOM behavior is verified rather than inferred from HTML text.

**Tech Stack:** Python 3.11, LangGraph 0.6.11, LangChain Core 0.3.86, FastAPI/Uvicorn, pytest/pytest-asyncio, Python Playwright with Chromium, existing HTML/CSS/JavaScript Demo, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-20-wallet-app-agent-evaluation-design.md`

## Global Constraints

- No private key, mnemonic, signer, or production wallet secret may enter requests, fixtures, logs, screenshots, or reports.
- Default evaluation must not sign or broadcast a mainnet transaction.
- Native assets use `address=None`; contract tokens still require trusted address and decimals metadata.
- Deterministic numeric, routing, state, and safety assertions must not rely on an LLM judge.
- Browser tests must assert real scroll geometry; source-string assertions are insufficient evidence.
- Preserve user-owned untracked files `.codegraph/` and `tests/test_demo_ui.py` and never stage them.
- Every behavior change follows red-green-refactor and is committed separately after its focused tests pass.

## File Structure

- `src/wallet_agent/graph/nodes.py`: recognize native swap assets and build `SwapQuoteRequest` objects whose native asset address is `None`.
- `tests/graph/test_graph_paths.py`: focused graph-boundary tests for native source/destination resolution and unknown assets.
- `src/evals/wallet_agent_evals.py`: deterministic three-turn regression scenario and dimension-level JSON aggregation.
- `tests/evals/test_wallet_agent_evals.py`: report and failed-conversation regression assertions.
- `demo/index.html`: fixed viewport/message-scroll layout and user-facing suppression of internal task-state narration.
- `tests/api/test_demo_page.py`: retain lightweight serving/contract checks, but stop treating source strings as proof of scrolling.
- `tests/browser/conftest.py`: isolated Uvicorn and Chromium fixtures for Demo browser tests.
- `tests/browser/test_demo_scroll.py`: real viewport tests for follow, pause, and resume behavior.
- `pyproject.toml`, `uv.lock`: Playwright development dependency and browser pytest marker.
- `docs/local-demo-debugging.md`: exact commands for installing Chromium and running each evaluation layer.

---

### Task 1: Resolve native assets before provider Token discovery

**Files:**
- Modify: `src/wallet_agent/graph/nodes.py:680-700`
- Modify: `src/wallet_agent/graph/nodes.py:1070-1248`
- Test: `tests/graph/test_graph_paths.py`

**Interfaces:**
- Consumes: `canonical_chain(value: str) -> str`, `canonical_symbol(value: str) -> str`, `chain_id_for(value: str) -> int | None`, and `_native_symbol(chain, wallet_context) -> str | None`.
- Produces: `_native_swap_asset(chain: str, symbol: str) -> Asset | None`; `_resolve_swap_assets(...)` fills decimals/chain ID for native assets without requiring an address; `_swap_draft_request(...)` accepts native `address=None`.

- [ ] **Step 1: Add failing native-destination and native-source tests**

Add imports for `_resolve_swap_assets` and `_swap_draft_request`, then add these tests to `tests/graph/test_graph_paths.py`:

```python
@pytest.mark.asyncio
async def test_swap_asset_resolution_accepts_native_destination_without_contract_address():
    resolved, candidates, errors = await _resolve_swap_assets(
        {
            "source_chain": "ETH",
            "source_symbol": "USDT",
            "source_token_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
            "source_decimals": 6,
            "destination_chain": "BSC",
            "destination_symbol": "BNB",
            "input_amount": "10",
        },
        {},
    )

    assert candidates == []
    assert errors == []
    assert resolved["destination_decimals"] == 18
    assert resolved["destination_chain_id"] == 56
    assert resolved.get("destination_token_address") is None

    request, missing = _swap_draft_request(
        resolved,
        {"address": "0x" + "1" * 40, "chain": "ETH", "chain_id": 1},
    )
    assert missing == []
    assert request is not None
    assert request.destination_asset.symbol == "BNB"
    assert request.destination_asset.address is None


@pytest.mark.asyncio
async def test_swap_asset_resolution_accepts_native_source_without_contract_address():
    resolved, candidates, errors = await _resolve_swap_assets(
        {
            "source_chain": "BSC",
            "source_symbol": "BNB",
            "destination_chain": "ETH",
            "destination_symbol": "USDT",
            "destination_token_address": "0xdac17f958d2ee523a2206206994597c13d831ec7",
            "destination_decimals": 6,
            "input_amount": "0.1",
        },
        {},
    )

    assert candidates == []
    assert errors == []
    assert resolved["source_decimals"] == 18
    assert resolved["source_chain_id"] == 56
    request, missing = _swap_draft_request(
        resolved,
        {"address": "0x" + "1" * 40, "chain": "BSC", "chain_id": 56},
    )
    assert missing == []
    assert request is not None
    assert request.input_amount_raw == "100000000000000000"
    assert request.source_asset.address is None
```

- [ ] **Step 2: Run the focused tests and verify the current resolver fails**

Run:

```bash
.venv/bin/pytest -q tests/graph/test_graph_paths.py -k 'native_destination or native_source'
```

Expected: both tests fail because the current resolver reports `ASSET_PROVIDER_UNAVAILABLE` or leaves the native address/decimals in the missing-field set.

- [ ] **Step 3: Add a native-asset metadata helper**

Add next to `_native_symbol` in `src/wallet_agent/graph/nodes.py`:

```python
_NATIVE_ASSET_DECIMALS = {
    "ETH": 18,
    "BASE": 18,
    "ARBITRUM": 18,
    "OPTIMISM": 18,
    "BSC": 18,
    "POLYGON": 18,
    "TRON": 6,
    "SOLANA": 9,
}


def _native_swap_asset(chain: str, symbol: str) -> Asset | None:
    canonical_chain_name = canonical_chain(chain)
    canonical_symbol_name = canonical_symbol(symbol)
    if _native_symbol(canonical_chain_name, None) != canonical_symbol_name:
        return None
    decimals = _NATIVE_ASSET_DECIMALS.get(canonical_chain_name)
    if decimals is None:
        return None
    return Asset(
        chain=canonical_chain_name,
        chain_id=chain_id_for(canonical_chain_name),
        symbol=canonical_symbol_name,
        decimals=decimals,
        address=None,
    )
```

- [ ] **Step 4: Make swap draft validation address-aware**

Refactor `_swap_draft_request` so its unconditional required tuple contains chains, symbols, decimals, sender, and recipient, but not token addresses. Append `<side>_token_address` only when `_native_swap_asset(chain, symbol)` returns `None`.

Use this exact shape before computing `missing`:

```python
required = [
    "source_chain",
    "destination_chain",
    "source_symbol",
    "destination_symbol",
    "source_decimals",
    "destination_decimals",
    "sender_address",
    "recipient_address",
]
for side in ("source", "destination"):
    chain = normalized.get(f"{side}_chain")
    symbol = normalized.get(f"{side}_symbol")
    if chain and symbol and _native_swap_asset(str(chain), str(symbol)) is None:
        required.append(f"{side}_token_address")
```

Build both asset dictionaries with `"address": normalized.get(...)`. Replace the same-asset comparison with a helper condition that compares chain plus canonical symbol for two native assets, and compares normalized addresses for contract tokens. This avoids treating `None` as a literal address.

- [ ] **Step 5: Resolve native sides without consulting Provider catalogs**

At the start of each side in `_resolve_swap_assets`, call `_native_swap_asset`. When it returns an asset, set canonical chain/symbol, decimals, optional chain ID, remove any stale token address with `resolved.pop(f"{side}_token_address", None)`, and continue to the next side.

Compute `resolvable_sides` using only sides that are not native. This ensures a native-to-native request does not require a configured asset Provider, while unresolved contract tokens still return `ASSET_PROVIDER_UNAVAILABLE`.

- [ ] **Step 6: Add the unknown-symbol safety regression**

Add:

```python
@pytest.mark.asyncio
async def test_swap_asset_resolution_does_not_treat_unknown_symbol_as_native():
    resolved, candidates, errors = await _resolve_swap_assets(
        {
            "source_chain": "BSC",
            "source_symbol": "NOTBNB",
            "destination_chain": "BSC",
            "destination_symbol": "BNB",
            "input_amount": "1",
        },
        {},
    )

    assert resolved["destination_decimals"] == 18
    assert candidates == []
    assert errors[0]["code"] == "ASSET_PROVIDER_UNAVAILABLE"
```

- [ ] **Step 7: Run the graph regression suite**

Run:

```bash
.venv/bin/pytest -q tests/graph/test_graph_paths.py tests/graph/test_task_memory.py
.venv/bin/ruff check src/wallet_agent/graph/nodes.py tests/graph
```

Expected: all selected tests pass and Ruff reports `All checks passed!`.

- [ ] **Step 8: Commit the native-asset fix**

```bash
git add src/wallet_agent/graph/nodes.py tests/graph/test_graph_paths.py
git commit -m "fix: resolve native assets in swap requests"
```

---

### Task 2: Turn the failed conversation into a scored deterministic evaluation

**Files:**
- Modify: `src/evals/wallet_agent_evals.py`
- Modify: `tests/evals/test_wallet_agent_evals.py`

**Interfaces:**
- Consumes: the Task 1 native-asset behavior and existing `EvalCase`, `_run_case`, `_run_suite`, and `run_offline_evals` entry points.
- Produces: `EvalCase.dimensions: tuple[str, ...]`; each case result contains `dimensions`; the report contains a `dimensions` aggregate and includes `swap_eth_usdt_to_bsc_bnb_multiturn`.

- [ ] **Step 1: Write report-shape and conversation regression assertions**

Update `tests/evals/test_wallet_agent_evals.py` so the expected total is 15 and add the new ID. Add:

```python
@pytest.mark.asyncio
async def test_offline_evals_score_native_asset_multiturn_regression():
    report = await run_offline_evals()
    case = next(
        item
        for item in report["cases"]
        if item["id"] == "swap_eth_usdt_to_bsc_bnb_multiturn"
    )

    assert case["passed"] is True
    assert case["response_kind"] == "swap_quote"
    assert "asset_and_chain_resolution" in case["dimensions"]
    assert report["dimensions"]["asset_and_chain_resolution"]["failed"] == 0
```

Also assert `report["summary"] == {"total": 15, "passed": 15, "pass_rate": 1.0}` and swap totals increase from 9 to 10.

- [ ] **Step 2: Run the evaluation test and verify it fails**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_agent_evals.py
```

Expected: failure because the new scenario and `dimensions` report do not exist.

- [ ] **Step 3: Generalize the fake asset catalog**

Change `FakeSwapProvider.assets` to use `(chain, symbol)` keys and include the assets needed by the real failed conversation:

```python
self.assets = {
    ("BASE", "USDC"): Asset(
        chain="BASE", chain_id=8453, symbol="USDC", decimals=6, address=USDC_ADDRESS
    ),
    ("BASE", "USDT"): Asset(
        chain="BASE", chain_id=8453, symbol="USDT", decimals=6, address=USDT_ADDRESS
    ),
    ("ETH", "USDT"): Asset(
        chain="ETH",
        chain_id=1,
        symbol="USDT",
        decimals=6,
        address="0xdac17f958d2ee523a2206206994597c13d831ec7",
    ),
}
```

Resolve `list_assets` with `(str(query.chain or "BASE").upper(), symbol)` and return an empty list when the key is absent. Do not add BNB to this catalog; the test must prove native resolution works without Provider Token metadata.

- [ ] **Step 4: Add dimension tags and aggregation**

Extend `EvalCase` with:

```python
dimensions: tuple[str, ...] = ()
```

Return `"dimensions": list(case.dimensions)` from `_run_case`. In `_run_suite`, aggregate every dimension present in results:

```python
dimension_names = sorted(
    {dimension for item in results for dimension in item.get("dimensions", [])}
)
dimensions = {}
for dimension in dimension_names:
    selected = [item for item in results if dimension in item.get("dimensions", [])]
    passed = sum(bool(item["passed"]) for item in selected)
    dimensions[dimension] = {
        "total": len(selected),
        "passed": passed,
        "failed": len(selected) - passed,
        "pass_rate": passed / len(selected),
    }
```

Include `"dimensions": dimensions` in the report. Tag the new case with `("conversation_understanding", "state_and_resume", "asset_and_chain_resolution")`. Existing cases may remain untagged in Phase 1; later plans will expand the matrix.

- [ ] **Step 5: Add the exact three-turn failure as an EvalCase**

Add:

```python
EvalCase(
    id="swap_eth_usdt_to_bsc_bnb_multiturn",
    capability="swap",
    turns=("把 10usdt 换成bnb", "用以太链上的usdt 换bnb", "目标链在bsc"),
    offline_outputs=(
        {
            "intent": "swap_quote",
            "source_symbol": "USDT",
            "destination_symbol": "BNB",
            "input_amount": "10",
        },
        {"intent": "swap_quote", "source_chain": "ETH"},
        {"intent": "swap_quote", "destination_chain": "BSC"},
    ),
    expected={
        "response_kind": "swap_quote",
        "equals": {
            "swap_request.source_asset.chain": "ETH",
            "swap_request.destination_asset.chain": "BSC",
            "swap_request.destination_asset.symbol": "BNB",
            "swap_request.destination_asset.address": None,
            "active_task.slots.input_amount": "10",
        },
        "side_effects": {"quote_calls": 1},
        "forbid_prepare": True,
        "forbid_broadcast": True,
    },
    dimensions=(
        "conversation_understanding",
        "state_and_resume",
        "asset_and_chain_resolution",
    ),
),
```

- [ ] **Step 6: Verify the evaluator API and command-line JSON**

Run:

```bash
.venv/bin/pytest -q tests/evals/test_wallet_agent_evals.py
.venv/bin/python -m evals.wallet_agent_evals
```

Expected: pytest passes; the CLI exits 0 and prints JSON with 15/15 passed plus the three dimension aggregates.

- [ ] **Step 7: Commit the deterministic regression evaluator**

```bash
git add src/evals/wallet_agent_evals.py tests/evals/test_wallet_agent_evals.py
git commit -m "test: evaluate native asset swap conversations"
```

---

### Task 3: Verify Demo scroll behavior in a real browser

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `demo/index.html:8-23`
- Modify: `demo/index.html:420-455`
- Modify: `tests/api/test_demo_page.py`
- Create: `tests/browser/conftest.py`
- Create: `tests/browser/test_demo_scroll.py`

**Interfaces:**
- Consumes: `wallet_agent.api.create_app()`, Uvicorn's `Server.run(sockets=[...])`, and `playwright.sync_api.sync_playwright`.
- Produces: `demo_url: str` and `demo_page: Page` pytest fixtures; browser assertions against `#messages`; a viewport-constrained Demo whose message container owns scrolling.

- [ ] **Step 1: Add Playwright to the development environment**

Run:

```bash
uv add --dev 'playwright>=1.55,<2'
.venv/bin/playwright install chromium
```

Verify `pyproject.toml` includes the dependency and `uv.lock` contains the resolved Playwright package. Add `browser: tests that require a locally installed Playwright Chromium` to the pytest markers list.

- [ ] **Step 2: Create reusable browser fixtures**

Create `tests/browser/conftest.py`:

```python
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from playwright.sync_api import Page, sync_playwright

from wallet_agent.api import create_app


@pytest.fixture(scope="session")
def demo_url() -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        pytest.fail("Demo Uvicorn server did not start")
    yield f"http://127.0.0.1:{port}/demo/"
    server.should_exit = True
    thread.join(timeout=5)
    listener.close()


@pytest.fixture()
def demo_page(demo_url: str) -> Iterator[Page]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(demo_url)
        yield page
        browser.close()
```

- [ ] **Step 3: Write the failing real-scroll test**

Create `tests/browser/test_demo_scroll.py`:

```python
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
```

- [ ] **Step 4: Run the browser test and verify the current layout fails**

Run:

```bash
.venv/bin/pytest -q tests/browser/test_demo_scroll.py
```

Expected: failure because `#messages.clientHeight` grows with content or its `scrollTop` cannot reach the content bottom while the document remains the actual scroll container.

- [ ] **Step 5: Constrain the application shell and message container**

Change the top-level styles in `demo/index.html` to:

```css
html, body { height: 100%; overflow: hidden; }
body { margin: 0; background: #0d1117; color: #edf2f7; }
main {
  width: min(760px, 100%);
  height: 100dvh;
  min-height: 0;
  margin: 0 auto;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  background: #111821;
}
#messages {
  flex: 1;
  min-height: 0;
  padding: 18px 14px 120px;
  overflow-y: auto;
  overscroll-behavior: contain;
}
```

Keep the existing scroll-follow JavaScript. It will now target the actual scroll container.

- [ ] **Step 6: Stop rendering internal task state as user chat**

The SSE debug panel already records complete events, so remove this call from `applyEvent`:

```javascript
if (shouldRender && current?.active_task) renderActiveTask(current.active_task);
```

Remove `renderActiveTask` and its `state.lastActiveTask` behavior from the normal chat path. Update `tests/api/test_demo_page.py` by removing assertions for `function renderActiveTask`, `task.revision`, and `task.missing_fields`; keep `active_task` only where it is part of the debug/event contract. Add:

```python
assert "height: 100dvh" in response.text
assert "min-height: 0" in response.text
assert "overscroll-behavior: contain" in response.text
assert "当前任务：${status}" not in response.text
```

This keeps implementation details available in `recordDebug` without presenting them as wallet guidance.

- [ ] **Step 7: Run browser and lightweight Demo tests**

Run:

```bash
.venv/bin/pytest -q tests/browser/test_demo_scroll.py tests/api/test_demo_page.py
.venv/bin/ruff check tests/browser tests/api/test_demo_page.py
```

Expected: both suites pass; the browser test demonstrates follow/pause/resume with measured geometry.

- [ ] **Step 8: Commit the verified scrolling behavior**

```bash
git add pyproject.toml uv.lock demo/index.html tests/api/test_demo_page.py tests/browser
git commit -m "test: verify demo scrolling in a real browser"
```

---

### Task 4: Document one-command evaluation and run the release gate

**Files:**
- Modify: `docs/local-demo-debugging.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: `python -m evals.wallet_agent_evals`, the `browser` pytest marker, and the browser fixtures from Task 3.
- Produces: documented deterministic and browser evaluation commands with explicit safety boundaries.

- [ ] **Step 1: Document environment setup and evaluation commands**

Add an “自动化 Wallet App 评测” section to `docs/local-demo-debugging.md` with these commands:

```bash
uv sync
.venv/bin/playwright install chromium
.venv/bin/python -m evals.wallet_agent_evals
.venv/bin/pytest -q tests/browser -m browser
```

State that the first command suite uses fake chain/provider backends, does not sign, and does not broadcast. State that `--online` invokes the configured language model but still uses simulated wallet backends. Add the two primary commands to the README test section.

- [ ] **Step 2: Run the focused Phase 1 verification**

Run:

```bash
.venv/bin/pytest -q tests/graph/test_graph_paths.py tests/graph/test_task_memory.py tests/evals/test_wallet_agent_evals.py tests/api/test_demo_page.py tests/browser/test_demo_scroll.py
.venv/bin/python -m evals.wallet_agent_evals
```

Expected: every test passes and the evaluator reports 15/15 with zero dimension failures.

- [ ] **Step 3: Run the full repository release gate**

Run:

```bash
.venv/bin/pytest -q --ignore=tests/test_config.py
.venv/bin/ruff check src tests --exclude=tests/test_demo_ui.py
.venv/bin/python -m compileall -q src tests
git diff --check
```

Expected: pytest has no failures, Ruff reports `All checks passed!`, compileall exits 0, and `git diff --check` prints nothing.

- [ ] **Step 4: Verify only intended files are staged**

Run:

```bash
git status --short
git diff --cached --name-only
```

Expected: `.codegraph/` and `tests/test_demo_ui.py` remain untracked and absent from the staged file list.

- [ ] **Step 5: Commit documentation and final Phase 1 integration**

```bash
git add README.md docs/local-demo-debugging.md
git commit -m "docs: add wallet agent evaluation workflow"
```

- [ ] **Step 6: Inspect final history and report evidence**

Run:

```bash
git log --oneline -5
git status --short
```

Report the exact test counts, evaluator summary, browser-test result, commit hashes, and any skipped tests. Do not claim Phase 2 Wallet lifecycle simulation or Phase 3 online/adversarial coverage is complete; those remain separate implementation plans under the approved design.
