from datetime import datetime, timezone

import pytest

from wallet_agent.domain.models import TransactionHistoryPage, TransactionStatus
from wallet_agent.okx.explorer import OkxExplorerAdapter

ADDRESS = "0x" + "1" * 40
TX_HASH = "0x" + "a" * 64


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def request(self, method, path, *, query=None, body=None):
        self.calls.append((method, path, query, body))
        return self.responses[path]


@pytest.mark.asyncio
async def test_history_maps_okx_transaction_list_and_cursor():
    client = FakeClient(
        {
            "/api/v6/dex/post-transaction/transactions-by-address": {
                "code": "0",
                "data": [
                    {
                        "cursor": "next",
                        "transactionList": [
                            {
                                "chainIndex": "1",
                                "txHash": TX_HASH,
                                "txStatus": "2",
                                "from": ADDRESS,
                                "to": "0x" + "2" * 40,
                                "amount": "10",
                                "blockHeight": "123",
                                "txTime": "1720000000000",
                                "methodId": "0xa9059cbb",
                            }
                        ],
                    }
                ],
            }
        }
    )
    adapter = OkxExplorerAdapter(client, {"ETH": "1"})

    page = await adapter.get_transaction_history(ADDRESS, "ETH", limit=2)

    assert isinstance(page, TransactionHistoryPage)
    assert page.next_cursor == "next"
    assert page.transactions[0].status is TransactionStatus.CONFIRMED
    assert page.transactions[0].source == "okx"
    assert page.transactions[0].history_kind == "full"
    assert page.transactions[0].method_id == "0xa9059cbb"
    assert page.transactions[0].confirmed_at == datetime.fromtimestamp(
        1720000000, tz=timezone.utc
    )
    assert client.calls[0] == (
        "GET",
        "/api/v6/dex/post-transaction/transactions-by-address",
        {"address": ADDRESS, "chains": "1", "limit": "2"},
        None,
    )


@pytest.mark.asyncio
async def test_history_treats_null_transactions_as_empty_page():
    client = FakeClient(
        {
            "/api/v6/dex/post-transaction/transactions-by-address": {
                "code": "0",
                "data": [{"cursor": "", "transactions": None}],
            }
        }
    )
    adapter = OkxExplorerAdapter(client, {"ETH": "1"})

    page = await adapter.get_transaction_history(ADDRESS, "ETH", limit=1)

    assert page.transactions == []
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_detail_maps_status_and_transaction_fields():
    client = FakeClient(
        {
            "/api/v6/dex/post-transaction/transaction-detail-by-txhash": {
                "code": "0",
                "data": [
                    {
                        "chainIndex": "1",
                        "txHash": TX_HASH,
                        "txStatus": "3",
                        "from": ADDRESS,
                        "to": "0x" + "2" * 40,
                        "amount": "10",
                        "blockHeight": "123",
                        "txTime": "1720000000000",
                        "gasLimit": "45528",
                        "nonce": "4",
                        "methodId": "0x095ea7b3",
                        "tokenTransfers": [{"token": "USDC", "amount": "10"}],
                    }
                ],
            }
        }
    )
    adapter = OkxExplorerAdapter(client, {"ETH": "1"})

    detail = await adapter.get_transaction_detail("ETH", TX_HASH)

    assert detail.status is TransactionStatus.FAILED
    assert detail.gas_limit == "45528"
    assert detail.nonce == "4"
    assert detail.token_transfers[0].token == "USDC"
    assert detail.token_transfers[0].amount == "10"
    assert await adapter.get_transaction_status("ETH", TX_HASH) is TransactionStatus.FAILED
    assert client.calls[0][1:] == (
        "/api/v6/dex/post-transaction/transaction-detail-by-txhash",
        {"txHash": TX_HASH, "chainIndex": "1"},
        None,
    )


@pytest.mark.asyncio
async def test_detail_uses_requested_hash_when_okx_omits_hash_and_marks_unknown():
    client = FakeClient(
        {
            "/api/v6/dex/post-transaction/transaction-detail-by-txhash": {
                "code": "0",
                "data": [{"chainIndex": "1"}],
            }
        }
    )
    adapter = OkxExplorerAdapter(client, {"ETH": "1"})

    detail = await adapter.get_transaction_detail("ETH", TX_HASH)

    assert detail.tx_hash == TX_HASH
    assert detail.status is TransactionStatus.UNKNOWN


@pytest.mark.asyncio
async def test_history_rejects_invalid_limit_and_unknown_chain():
    adapter = OkxExplorerAdapter(FakeClient({}), {"ETH": "1"})
    with pytest.raises(Exception, match="limit"):
        await adapter.get_transaction_history(ADDRESS, "ETH", limit=101)
    with pytest.raises(Exception, match="supported"):
        await adapter.get_transaction_history(ADDRESS, "NOPE")


@pytest.mark.asyncio
async def test_successful_history_and_detail_calls_are_cached_by_complete_query():
    client = FakeClient(
        {
            "/api/v6/dex/post-transaction/transactions-by-address": {
                "code": "0",
                "data": [{"cursor": "", "transactionList": []}],
            },
            "/api/v6/dex/post-transaction/transaction-detail-by-txhash": {
                "code": "0",
                "data": [{"chainIndex": "1", "txHash": TX_HASH, "txStatus": "1"}],
            },
        }
    )
    adapter = OkxExplorerAdapter(client, {"ETH": "1"})

    await adapter.get_transaction_history(ADDRESS, "ETH", limit=2)
    await adapter.get_transaction_history(ADDRESS, "ETH", limit=2)
    await adapter.get_transaction_history(ADDRESS, "ETH", limit=3)
    await adapter.get_transaction_detail("ETH", TX_HASH)
    await adapter.get_transaction_detail("ETH", TX_HASH)

    assert len(client.calls) == 3
