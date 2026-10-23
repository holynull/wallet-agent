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
async def test_history_carries_token_logo_and_symbol():
    client = FakeClient(
        {
            "/api/v6/dex/post-transaction/transactions-by-address": {
                "code": "0",
                "data": [
                    {
                        "cursor": "",
                        "transactionList": [
                            {
                                "chainIndex": "1",
                                "txHash": TX_HASH,
                                "txStatus": "2",
                                "from": ADDRESS,
                                "to": "0x" + "2" * 40,
                                "amount": "10",
                                "symbol": "USDC",
                                "tokenLogoUrl": "https://static.okx.com/usdc.png",
                            }
                        ],
                    }
                ],
            }
        }
    )
    adapter = OkxExplorerAdapter(client, {"ETH": "1"})

    page = await adapter.get_transaction_history(ADDRESS, "ETH", limit=1)

    record = page.transactions[0]
    assert record.symbol == "USDC"
    assert record.logo_url == "https://static.okx.com/usdc.png"


@pytest.mark.asyncio
async def test_history_accepts_okx_address_lists_and_keeps_our_own_side():
    """OKX sends from/to as [{address, amount}]; a 500 here used to hide all EVM history."""
    other = "0x" + "2" * 40
    client = FakeClient(
        {
            "/api/v6/dex/post-transaction/transactions-by-address": {
                "code": "0",
                "data": [
                    {
                        "transactionList": [
                            {
                                "chainIndex": "1",
                                "txHash": TX_HASH,
                                "txStatus": "success",
                                "from": [{"address": other, "amount": ""}],
                                "to": [{"address": ADDRESS, "amount": ""}],
                                "amount": "2000000",
                                "symbol": "ETHG",
                                "txTime": "1791248687000",
                            },
                            {
                                "chainIndex": "1",
                                "txHash": "0x" + "b" * 64,
                                "txStatus": "success",
                                "from": [{"address": ADDRESS, "amount": ""}],
                                "to": [
                                    {"address": other, "amount": ""},
                                    {"address": "0x" + "3" * 40, "amount": ""},
                                ],
                                "amount": "1000",
                                "symbol": "ETH",
                                "txTime": "1791248687001",
                            },
                        ]
                    }
                ],
            }
        }
    )
    adapter = OkxExplorerAdapter(client, {"ETH": "1"})

    page = await adapter.get_transaction_history(ADDRESS, "ETH", limit=5)

    incoming, outgoing = page.transactions
    # Our own address stays on the side it appears on, which is what the
    # history card uses to tell send from receive.
    assert incoming.from_address == other
    assert incoming.to_address == ADDRESS
    assert incoming.symbol == "ETHG"
    assert incoming.occurred_at == datetime.fromtimestamp(1791248687, tz=timezone.utc)
    assert outgoing.from_address == ADDRESS
    assert outgoing.to_address == other


def test_record_keeps_a_provider_failure_reason():
    from wallet_agent.domain.models import TransactionStatus

    adapter = OkxExplorerAdapter(FakeClient({}), {"ETH": "1"})

    failed = adapter._record(
        {"chainIndex": "1", "txHash": TX_HASH, "txStatus": "fail", "failReason": "out of gas"},
        history_kind="full",
    )
    assert failed.status is TransactionStatus.FAILED
    assert failed.failure_reason == "out of gas"

    # No reason in the payload must stay None instead of being invented.
    silent = adapter._record(
        {"chainIndex": "1", "txHash": TX_HASH, "txStatus": "fail"}, history_kind="full"
    )
    assert silent.failure_reason is None



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
async def test_detail_parses_okx_token_transfer_details_and_address_lists():
    """OKX detail uses tokenTransferDetails + fromDetails/toDetails (not
    tokenTransfers/from/to). A prior parser read the wrong keys, so ERC20
    transfers surfaced value=0, empty token_transfers, and null sides."""
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    sender = "0x" + "1" * 40
    recipient = "0x" + "2" * 40
    client = FakeClient(
        {
            "/api/v6/dex/post-transaction/transaction-detail-by-txhash": {
                "code": "0",
                "data": [
                    {
                        "chainIndex": "1",
                        "txHash": TX_HASH,
                        "txStatus": "2",
                        "amount": "0",
                        "symbol": "ETH",
                        "methodId": "0xa9059cbb",
                        "fromDetails": [{"address": sender, "isContract": False, "amount": ""}],
                        "toDetails": [{"address": recipient, "isContract": True, "amount": ""}],
                        "tokenTransferDetails": [
                            {
                                "from": sender,
                                "to": recipient,
                                "isFromContract": False,
                                "isToContract": True,
                                "tokenContractAddress": usdt,
                                "symbol": "USDT",
                                "amount": "10",
                            }
                        ],
                    }
                ],
            }
        }
    )
    adapter = OkxExplorerAdapter(client, {"ETH": "1"})

    detail = await adapter.get_transaction_detail("ETH", TX_HASH)

    assert detail.value == "0"
    assert detail.symbol == "ETH"
    assert detail.from_address == sender
    assert detail.to_address == recipient
    assert len(detail.token_transfers) == 1
    transfer = detail.token_transfers[0]
    assert transfer.token == usdt
    assert transfer.symbol == "USDT"
    assert transfer.amount == "10"
    assert transfer.from_address == sender
    assert transfer.to_address == recipient
    # asset_logo_url keys Trust Wallet by the EIP-55 checksummed directory, so
    # the logo points at the real (non-404) path even though the explorer
    # reported the contract address in lowercase.
    assert transfer.logo_url == (
        "https://raw.githubusercontent.com/trustwallet/assets/master/"
        "blockchains/ethereum/assets/"
        "0xdAC17F958D2ee523a2206206994597C13D831ec7/logo.png"
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
