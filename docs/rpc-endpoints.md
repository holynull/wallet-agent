# Public RPC defaults

These endpoints are useful for development and read-only smoke tests. They are
public, rate-limited, and provide no availability or throughput guarantee. Use
a managed/private RPC provider for production and never put provider API keys
in wallet-agent requests or checkpoints.

| Network | Public endpoint | Notes |
| --- | --- | --- |
| Ethereum | `https://cloudflare-eth.com` | Cloudflare public gateway |
| Base | `https://mainnet.base.org` | Base community endpoint |
| Arbitrum One | `https://arb1.arbitrum.io/rpc` | Arbitrum public endpoint |
| Optimism | `https://mainnet.optimism.io` | Optimism public endpoint |
| Polygon | `https://polygon-rpc.com` | Polygon community endpoint |
| BNB Smart Chain | `https://bsc-dataseed.binance.org` | Binance public endpoint |
| TRON | `https://api.trongrid.io` | REST API; may be rate-limited |
| Solana | `https://api.mainnet-beta.solana.com` | Solana public JSON-RPC |

The values are included in `.env.example` as `RPC_URLS`. Public endpoints can
return `429`, time out, or lag; configure bounded retries and a fallback RPC in
the deployment layer.
