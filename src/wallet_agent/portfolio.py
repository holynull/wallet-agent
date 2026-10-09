"""App-scoped, address-family-aware balance queries (no intent classification)."""

from __future__ import annotations

import asyncio
import hashlib
import re
from decimal import Decimal, localcontext
from time import monotonic
from typing import Any

from wallet_agent.chains.solana import _base58_bytes
from wallet_agent.chains.tron import _valid_tron
from wallet_agent.domain.normalization import canonical_chain, canonical_symbol
from wallet_agent.okx import OKX_CHAIN_INDEX_BY_NAME

EVM_NETWORKS = ("ETH", "BSC", "BASE", "POLYGON", "ARBITRUM", "OPTIMISM")


def valid_btc_address(address: str) -> bool:
    """Bitcoin mainnet Base58Check and BIP173/BIP350 witness addresses."""
    try:
        if address.startswith(("1", "3")):
            raw = _base58_bytes(address)
            return (
                len(raw) == 25
                and raw[0] in (0, 5)
                and hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] == raw[-4:]
            )
        if address != address.lower() and address != address.upper():
            return False
        value = address.lower()
        if not value.startswith("bc1") or len(value) > 90:
            return False
        alphabet = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
        data = [alphabet.index(char) for char in value[3:]]
        if len(data) < 7 or data[0] > 16:
            return False
        checksum = 1
        for part in [3, 3, 0, 2, 3, *data]:  # HRP expansion of 'bc'
            high = checksum >> 25
            checksum = ((checksum & 0x1FFFFFF) << 5) ^ part
            for index, generator in enumerate(
                (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
            ):
                if (high >> index) & 1:
                    checksum ^= generator
        if checksum != (1 if data[0] == 0 else 0x2BC830A3):
            return False
        bits = 0
        accumulator = 0
        program: list[int] = []
        for part in data[1:-6]:
            accumulator = (accumulator << 5) | part
            bits += 5
            if bits >= 8:
                bits -= 8
                program.append((accumulator >> bits) & 255)
        return (
            bits < 5
            and not ((accumulator << (8 - bits)) & 255)
            and 2 <= len(program) <= 40
            and (data[0] != 0 or len(program) in (20, 32))
        )
    except (ValueError, IndexError):
        return False


def valid_address(chain: str, address: str) -> bool:
    if chain in EVM_NETWORKS:
        return bool(re.fullmatch(r"0x[0-9a-fA-F]{40}", address))
    if chain == "BTC":
        return valid_btc_address(address)
    if chain == "TRON":
        return _valid_tron(address)
    if chain == "SOLANA":
        try:
            return len(_base58_bytes(address)) == 32
        except ValueError:
            return False
    return False


async def query_portfolio(
    provider: Any,
    context: dict[str, Any],
    arguments: dict[str, Any],
    *,
    price_provider: Any = None,
    timeout: float = 15,
) -> dict[str, Any]:
    """Fan out within one time budget; failed accounts never erase successful ones."""
    if provider is None or not hasattr(provider, "get_token_balances"):
        return {
            "ok": False,
            "code": "OKX_CAPABILITY_UNAVAILABLE",
            "error": "OKX 钱包数据服务暂不可用。",
        }
    deadline = monotonic() + timeout + 2
    indexes = {
        canonical_chain(k): str(v)
        for k, v in (
            getattr(provider, "chain_index_by_name", None) or OKX_CHAIN_INDEX_BY_NAME
        ).items()
    }
    explicit = canonical_chain(str(arguments.get("chain") or ""))
    if explicit == "ALL":
        explicit = ""
    app_scope = context.get("supported_networks")
    accounts = context.get("wallet_accounts")
    legacy_address = str(context.get("address") or arguments.get("address") or "")
    legacy_chain = canonical_chain(str(context.get("chain") or arguments.get("chain") or ""))
    if app_scope is None:
        # Old clients may only provide the connected account. Never reuse an
        # EVM account on non-EVM chains, even when the provider supports them.
        app_scope = (
            [chain for chain in EVM_NETWORKS if chain in indexes]
            if valid_address("ETH", legacy_address)
            else [legacy_chain]
            if legacy_chain
            else []
        )
        if accounts:
            app_scope = list(
                dict.fromkeys([*app_scope, *(canonical_chain(item["chain"]) for item in accounts)])
            )
    supported = list(dict.fromkeys(canonical_chain(str(chain)) for chain in app_scope))
    chains = [explicit] if explicit else supported
    if accounts is None:
        accounts = [
            {"chain": chain, "address": legacy_address}
            for chain in supported
            if valid_address(chain, legacy_address)
        ]
    requested_address = str(arguments.get("address") or "").strip()
    symbol = canonical_symbol(str(arguments.get("symbol") or ""))
    statuses: list[dict[str, Any]] = []
    jobs: list[tuple[str, str, str]] = []
    for chain in chains:
        addresses = list(
            dict.fromkeys(
                str(item.get("address") or "").strip()
                for item in accounts
                if canonical_chain(str(item.get("chain") or "")) == chain
            )
        )
        if requested_address:
            addresses = [requested_address]
        status: dict[str, Any] = {"chain": chain, "addresses": addresses, "accounts": []}
        statuses.append(status)
        if chain not in supported or chain not in indexes:
            status["status"] = "unsupported"
        elif not addresses or not any(addresses):
            status["status"] = "missing_address"
        else:
            for address in addresses:
                if not valid_address(chain, address):
                    status["accounts"].append({"address": address, "status": "invalid_address"})
                else:
                    jobs.append((chain, address, indexes[chain]))
    semaphore = asyncio.Semaphore(3)

    async def fetch(job: tuple[str, str, str]) -> tuple[list[Any], dict[str, Any]]:
        chain, address, index = job
        async with semaphore:
            try:
                if hasattr(provider, "get_balance_snapshot"):
                    result = await provider.get_balance_snapshot(address, [index])
                    balances, errors = result["balances"], result.get("errors") or []
                else:
                    balances, errors = await provider.get_token_balances(address, [index]), []
                return balances, {
                    "address": address,
                    "status": "partial" if errors else "success",
                    "errors": errors,
                }
            except Exception as exc:
                return [], {
                    "address": address,
                    "status": "failed",
                    "code": getattr(exc, "code", "BALANCE_QUERY_FAILED"),
                }

    tasks = [asyncio.create_task(fetch(job)) for job in jobs]
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    with localcontext() as ctx:
        ctx.prec = 512
        for job, task in zip(jobs, tasks):
            chain, address, _ = job
            if task.cancelled():
                balances, account = (
                    [],
                    {"address": address, "status": "failed", "code": "BALANCE_QUERY_TIMEOUT"},
                )
            else:
                balances, account = task.result()
            next(item for item in statuses if item["chain"] == chain)["accounts"].append(account)
            missing_prices = [
                item for item in balances if item.usd_value is None and item.amount > 0
            ]
            prices = []
            if missing_prices and price_provider is not None and monotonic() < deadline:
                try:
                    prices = await asyncio.wait_for(
                        price_provider.get_prices([item.asset for item in missing_prices]),
                        timeout=max(0.001, min(2, deadline - monotonic())),
                    )
                except Exception:
                    pass
            price_map = {
                (
                    canonical_chain(item.asset.chain),
                    canonical_symbol(item.asset.symbol),
                    item.asset.address,
                ): item.usd_price
                for item in prices
            }
            for balance in balances:
                asset = balance.asset
                if canonical_chain(asset.chain) != chain:
                    account["status"] = "partial"
                    account.setdefault("errors", []).append({"code": "BALANCE_CHAIN_MISMATCH"})
                    continue
                if symbol and canonical_symbol(asset.symbol) != symbol:
                    continue
                usd = balance.usd_value
                if usd is None:
                    price = price_map.get((chain, canonical_symbol(asset.symbol), asset.address))
                    if price is not None:
                        usd = balance.amount * price
                if balance.amount <= 0 or (usd is not None and usd <= 0):
                    continue
                identity = (
                    chain,
                    canonical_symbol(asset.symbol),
                    (asset.address or "").lower() if chain in EVM_NETWORKS else asset.address or "",
                )
                if identity not in rows:
                    rows[identity] = {
                        "asset": {**asset.model_dump(mode="json"), "chain": chain},
                        "amount": Decimal(0),
                        "amount_raw": 0,
                        "usd_value": Decimal(0),
                        "addresses": [],
                    }
                row = rows[identity]
                row["amount"] += balance.amount
                row["amount_raw"] += int(balance.amount_raw)
                row["usd_value"] = (
                    row["usd_value"] + usd
                    if row["usd_value"] is not None and usd is not None
                    else None
                )
                row["addresses"].append(address)
        assets = sorted(
            rows.values(), key=lambda item: item["usd_value"] or Decimal(0), reverse=True
        )
        total = sum(
            (item["usd_value"] for item in assets if item["usd_value"] is not None), Decimal(0)
        )
        for item in assets:
            for field in ("amount", "amount_raw", "usd_value"):
                if item[field] is not None:
                    item[field] = (
                        format(item[field], "f")
                        if isinstance(item[field], Decimal)
                        else str(item[field])
                    )
    for status in statuses:
        if "status" not in status:
            values = [item["status"] for item in status["accounts"]]
            status["status"] = (
                "success"
                if all(value == "success" for value in values)
                else "partial"
                if any(value in ("success", "partial") for value in values)
                else "invalid_address"
                if all(value == "invalid_address" for value in values)
                else "failed"
            )
    complete = bool(statuses) and all(item["status"] == "success" for item in statuses)
    native = next((item for item in assets if explicit and not item["asset"].get("address")), None)
    wallet = {
        "address": legacy_address,
        "chain": explicit or "ALL",
        "native_balance": native,
        "token_balances": [item for item in assets if item is not native],
        "source": "okx",
    }
    return {
        "ok": True,
        "portfolio": {
            "wallet": wallet,
            "assets": assets,
            "asset_count": len(assets),
            "chains": chains,
            "scope": {"requested": "specified" if explicit else "all", "resolved_chains": chains},
            "asset_scope": {"symbol": symbol} if symbol else None,
            "network_status": statuses,
            "complete": complete,
            "valuation_complete": all(item["usd_value"] is not None for item in assets),
            "total_usd_value": format(total, "f"),
            "total_source": "displayed_assets",
            "address_scope": (
                "App supplied public addresses; BTC includes only supplied addresses, "
                "not an entire HD wallet"
            ),
        },
    }
