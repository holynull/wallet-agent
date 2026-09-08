# LangGraph 文档 MCP

本项目为 LangGraph/LangChain 开发配置了 `context7` 文档 MCP：

```toml
[mcp_servers.context7]
url = "https://mcp.context7.com/mcp"
```

## 用途

它用于开发时按库和版本检索官方文档，例如核对 `StateGraph`、checkpointer、`interrupt`、`Command`、streaming 和 LangChain tools 的 API。它不是业务 Agent 的运行时工具，也不应把文档内容写入图状态。

## 首次授权

Context7 当前需要 OAuth。执行以下命令会显示授权 URL：

```bash
codex mcp login context7
```

在浏览器完成授权后，重启 Codex 会话。检查配置：

```bash
codex mcp get context7
codex mcp list
```

## 使用规则

查询前先确认项目中的 Python/TypeScript、`langgraph`、`langchain-core` 和相关 adapter 版本。查询结果要与 lockfile、已安装包和测试交叉验证；如果 Context7 不可用，改用官方文档 Fetch/Browser，并明确说明回退来源。

## 安全边界

不要在 MCP 配置、查询或日志中写入 API Key、用户数据或完整生产 prompt。只为需要的文档主题发起查询，避免把整套文档灌入运行时上下文。
