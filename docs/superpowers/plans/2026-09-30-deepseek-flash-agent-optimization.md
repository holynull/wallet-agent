# DeepSeek Flash Agent Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use DeepSeek Flash's role-specific reasoning, parallel read-only tool calls, cache-aware observability, and safer tool schemas without exposing write capabilities to the model.

**Architecture:** Keep deterministic wallet actions and confirmation gates unchanged. Configure separate LangChain clients for fast structured roles and the native read-only agent, make the read-only loop explicitly support bounded parallel calls and duplicate-call termination, and carry provider usage metadata through the existing `model_trace` SSE/state contract into the new Demo.

**Tech Stack:** Python 3.11, LangGraph 0.6.11, LangChain Core 0.3.86, LangChain OpenAI 0.3.35, DeepSeek OpenAI-compatible Chat Completions, FastAPI SSE, Next.js 12/React 17.

**Spec:** DeepSeek official Tool Calls, Chat Completions, Thinking Mode, and Context Caching documentation queried through Context7 on 2026-09-30.

## Global Constraints

- The model may call only the fixed read-only tools; transfer, approval, swap preparation, signing, and broadcast remain deterministic graph nodes.
- Preserve existing `model_trace`, `read_tool_calls`, SSE recovery, checkpoint serialization, and browser UI contracts.
- Do not expose `reasoning_content` to the user or persist private credentials.
- Bound a native read round to four parallel tool calls and terminate repeated calls using verified signatures.
- Use `response_format=json_object` only for structured non-tool roles; native read calls continue through `bind_tools`.

### Task 1: Role-specific DeepSeek client profiles and usage metadata

**Files:**
- Modify: `src/wallet_agent/config.py`
- Modify: `src/wallet_agent/main.py`
- Modify: `src/wallet_agent/models/registry.py`
- Test: `tests/test_config.py`
- Test: `tests/models/test_registry.py`

**Interfaces:**
- Settings expose `deepseek_reasoning_effort`, `deepseek_thinking_enabled`, `read_agent_reasoning_effort`, and `read_agent_thinking_enabled` with low-latency defaults for structured roles and low-effort thinking for the read agent.
- `ModelRouter._invoke` appends normalized `finish_reason`, prompt/completion/total tokens, and cached prompt tokens to each model trace entry when provider metadata supplies them.

- [x] Write failing tests for profile defaults, read-tool binder kwargs, and normalized usage metadata.
- [x] Run the focused config and registry tests and confirm they fail for the missing settings/trace fields.
- [x] Add role-specific client construction in `build_application`; use disabled thinking and `reasoning_effort=none` for classifier/extractor/response clients, and enabled low-effort thinking for raw read clients.
- [x] Update `_invoke` to normalize `response_metadata`/`usage_metadata` without serializing reasoning content.
- [x] Run focused tests, then the existing model and config test modules.

### Task 2: Bounded parallel native tools and duplicate-call termination

**Files:**
- Modify: `src/wallet_agent/graph/read_only.py`
- Modify: `src/wallet_agent/graph/nodes.py`
- Modify: `src/wallet_agent/models/registry.py`
- Test: `tests/graph/test_read_only_loop.py`
- Test: `tests/models/test_registry.py`

**Interfaces:**
- Add `READ_ONLY_MAX_PARALLEL_TOOLS = 4` and a canonical `read_tool_signature(name, args)` helper.
- `ModelRouter.invoke_read_tools` calls `bind_tools(tools, tool_choice="auto", parallel_tool_calls=True)` with a compatibility fallback for injected test doubles.
- The native read node rejects more than four calls and ends a round when every requested signature already completed successfully, reusing verified facts instead of re-running the provider.

- [x] Add failing tests for binder kwargs, over-limit rejection, and repeated-call termination.
- [x] Run the focused graph/model tests and confirm the new behavior fails.
- [x] Implement bounded validation, signature tracking in `task_plan`, and repeated-call termination while preserving existing ToolMessage IDs.
- [x] Run focused graph/model tests and the full non-browser graph/API test set.

### Task 3: Safer read-only tool schemas and prompt/cache alignment

**Files:**
- Modify: `src/wallet_agent/graph/read_only.py`
- Modify: `src/wallet_agent/models/registry.py`
- Test: `tests/graph/test_read_only_loop.py`
- Test: `tests/models/test_understanding.py`

**Interfaces:**
- Tool descriptions explicitly instruct the model to use symbol and chain first, resolve token metadata server-side, and never ask users for decimals or contract addresses when a resolver can determine them.
- Static role instructions use `SystemMessage` with dynamic request JSON in a trailing `HumanMessage` so DeepSeek prefix caching can reuse the stable prefix.

- [x] Add failing tests asserting the tool descriptions contain resolver/no-decimals guidance and model inputs separate system instructions from dynamic payloads.
- [x] Run the focused tests and confirm current prompts fail those assertions.
- [x] Update tool descriptions and message construction without changing the structured output schemas.
- [x] Run the focused tests and prompt-related regression tests.

### Task 4: Surface model cost/latency diagnostics in the new Demo

**Files:**
- Modify: `demo-next/lib/agent-utils.ts`
- Modify: `demo-next/app/page.tsx`
- Modify: `demo-next/app/globals.css`
- Test: `demo-next/test/agent-utils.test.mjs`

**Interfaces:**
- Preserve existing model/tool timeline fields and add optional usage/cache/finish metadata to model trace rendering.
- Redact debug output as before; only numeric usage and provider status are shown in the normal UI.

- [x] Add failing utility tests for model trace metadata formatting and missing usage fields.
- [x] Run the Demo unit test and confirm the new assertions fail.
- [x] Extend model trace types and render compact token/cache diagnostics beside the model ID and elapsed time.
- [x] Run Demo tests, `npm run build`, `git diff --check`, and the relevant Python test suite.

## Verification

- Focused Python tests for config, model router, and read-only graph pass.
- Existing API/SSE/graph regression tests pass.
- Demo unit tests pass and Next.js production build succeeds.
- `ruff check src tests` and `git diff --check` are clean.
