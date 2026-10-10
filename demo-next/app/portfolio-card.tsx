import { formatBalanceUsd } from "../lib/agent-utils";

const statusLabels: Record<string, string> = { success: "查询成功", partial: "部分成功", failed: "查询失败", missing_address: "未提供地址", invalid_address: "地址无效", unsupported: "暂不支持" };

export function PortfolioCard({ portfolio }: { portfolio: any }) {
  const assets = portfolio.assets || [];
  const hasAssets = portfolio.has_assets ?? assets.length > 0;
  return <section className="card portfolio-card" data-card-kind="portfolio" data-has-assets={hasAssets}>
    <div className="progress-heading"><h3>多链资产总览</h3><span>{portfolio.complete ? "查询完整" : "查询不完整"}</span></div>
    <p className="portfolio-total" data-testid="portfolio-total">$ {formatBalanceUsd(portfolio.total_usd_value)}</p>
    {hasAssets ? <ul className="asset-list">{assets.map((item: any, index: number) => <li key={`${item.asset?.chain}-${item.asset?.address || item.asset?.symbol}-${index}`} data-testid="portfolio-asset">
      <span className="asset-name">{item.asset?.symbol} <em>({item.asset?.chain})</em></span>
      {item.unit_price != null && <span className="asset-price">单价 $ {formatBalanceUsd(item.unit_price)}</span>}
      <span className="asset-amount">{item.amount} · $ {formatBalanceUsd(item.usd_value)}</span>
    </li>)}</ul> : <p className="muted" data-testid="portfolio-empty">暂无资产数据</p>}
    {portfolio.valuation_complete === false && <p className="muted">部分资产价格未知，合计仅含已估值资产。</p>}
    {portfolio.network_status?.length > 0 && <ul className="coverage-list">{portfolio.network_status.map((item: any) => <li key={item.chain} data-status={item.status}><strong>{item.chain}</strong><span>{statusLabels[item.status] || item.status}{item.addresses?.length ? ` · ${item.addresses.length} 个地址` : ""}</span></li>)}</ul>}
    {!portfolio.complete && <p className="muted">缺少地址或查询失败的网络不代表余额为零，以上合计仅来自已查询明细。</p>}
    {portfolio.chains?.includes("BTC") && <p className="muted">BTC 仅覆盖 App 提供的公共地址。完整钱包余额需要 App 提供全部相关收款及找零地址。</p>}
  </section>;
}
