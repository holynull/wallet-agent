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

## RPC and OKX responsibility split

When OKX enhancement is enabled, direct RPC remains authoritative for native
and token spending balances, allowances, receipts, pending transactions, and
account nonces. OKX gas-limit and simulation responses are additional preflight
evidence. The prepared transaction uses the largest observed gas requirement
and reports `gas_sources` (`rpc`, `okx`, `simulation`) so clients can explain
the result. An explicit simulation failure blocks signing; an unavailable
simulation is a warning and does not replace successful RPC checks.

For EIP-1559 chains, a zero `eth_maxPriorityFeePerGas` is not copied into the
wallet request. The adapter first consults `eth_feeHistory`, then uses a bounded
positive fallback, while enforcing
`max_fee_per_gas >= base_fee_per_gas + max_priority_fee_per_gas`.

After a wallet returns a hash, receipt and transaction lookups are retried for
a bounded interval. The source visibility taxonomy is `broadcast_seen`,
`broadcast_pending`, `not_propagated`, `confirmed`, `failed`,
`dropped_or_replaced`, or `unknown`. Provider polling starts only for
`broadcast_seen`, `broadcast_pending`, or `confirmed`; an RPC-missing hash is
never presented as “Provider processing.”
