#!/usr/bin/env python3
"""Regenerate the verified Trust Wallet token-logo registry.

Trust Wallet changes chain slugs and token directories (e.g. BSC went from
``binance`` to ``smartchain``; a guessed URL then 404s and the App loses the
logo). This script pulls each chain's ``tokenlist.json`` and writes the exact
raw GitHub logo URL for every listed token, so the backend only ever emits URLs
Trust Wallet actually publishes.

Run after Trust Wallet changes, or on a schedule:

    python scripts/sync_trustwallet_logos.py
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from wallet_agent.domain.chains import _evm_checksum_address

BASE = "https://raw.githubusercontent.com/trustwallet/assets/master/blockchains"
# Token directories use these slugs (BSC's chain badge is still ``binance``,
# but its per-token assets moved to ``smartchain``).
TOKEN_SLUGS = {
    "ETH": "ethereum",
    "BSC": "smartchain",
    "BASE": "base",
    "ARBITRUM": "arbitrum",
    "OPTIMISM": "optimism",
    "POLYGON": "polygon",
    "TRON": "tron",
    "SOLANA": "solana",
    "AVALANCHE": "avalanchec",
    "CELO": "celo",
    "MEGAETH": "megaeth",
}

OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "src/wallet_agent/domain/trustwallet_token_logos.json"
)


def _fetch_tokenlist(slug: str) -> list[dict]:
    url = f"{BASE}/{slug}/tokenlist.json"
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.load(response)
    return list(payload.get("tokens") or [])


def main() -> int:
    registry: dict[str, dict[str, str]] = {}
    for canonical, slug in TOKEN_SLUGS.items():
        try:
            tokens = _fetch_tokenlist(slug)
        except Exception as exc:
            print(f"skip {canonical} ({slug}): {exc}")
            continue
        chain_logos: dict[str, str] = {}
        for token in tokens:
            address = str(token.get("address") or "").strip()
            if not address:
                continue
            display = (
                _evm_checksum_address(address)
                if address.startswith("0x")
                else address
            )
            chain_logos[address.lower()] = (
                f"{BASE}/{slug}/assets/{display}/logo.png"
            )
        registry[canonical] = dict(sorted(chain_logos.items()))
        print(f"{canonical}: {len(chain_logos)} tokens")
    OUTPUT.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
