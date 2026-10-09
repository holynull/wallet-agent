import { formatBalanceUsd } from "../lib/agent-utils";

const statusLabels: Record<string, string> = { success: "查询成功", partial: "部分成功", failed: "查询失败", missing_address: "未提供地址", invalid_address: "地址无效", unsupported: "暂不支持" };

export function PortfolioCard({ portfolio }: { portfolio: any }) {
  const assets = portfolio.assets || [];
  return <section className="card portfolio-card" data-card-kind="portfolio">
    <div className="progress-heading"><h3>{portfolio.asset_scope?.symbol ? `${portfolio.asset_scope.symbol} 余额` : "多链余额"}</h3><span>{portfolio.complete ? "查询完整" : "查询不完整"}</span></div>
    <p>已查询资产合计：<strong>{formatBalanceUsd(portfolio.total_usd_value)} USD</strong> · {assets.length} 项资产{portfolio.valuation_complete === false ? "（部分资产价格未知）" : ""}</p>
    {portfolio.network_status?.length > 0 && <ul className="coverage-list">{portfolio.network_status.map((item: any) => <li key={item.chain} data-status={item.status}><strong>{item.chain}</strong><span>{statusLabels[item.status] || item.status}{item.addresses?.length ? ` · ${item.addresses.length} 个地址` : ""}</span></li>)}</ul>}
    {!portfolio.complete && <p className="muted">缺少地址或查询失败的网络不代表余额为零，以上合计仅来自已查询明细。</p>}
    {assets.length > 0 ? <div className="markdown-table-wrap"><table><thead><tr><th>网络</th><th>资产</th><th>数量</th><th>价值（USD）</th></tr></thead><tbody>{assets.map((item: any, index: number) => <tr key={`${item.asset?.chain}-${item.asset?.address || item.asset?.symbol}-${index}`}><td>{item.asset?.chain}</td><td title={item.asset?.address || "原生资产"}>{item.asset?.symbol}</td><td>{item.amount}</td><td>{formatBalanceUsd(item.usd_value)}</td></tr>)}</tbody></table></div> : <p>当前查询范围内未返回有价值的资产。</p>}
    {portfolio.chains?.includes("BTC") && <p className="muted">BTC 仅覆盖 App 提供的公共地址。完整钱包余额需要 App 提供全部相关收款及找零地址。</p>}
  </section>;
}
