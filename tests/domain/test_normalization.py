from wallet_agent.domain.normalization import (
    canonical_amount,
    canonical_chain,
    canonical_symbol,
    chain_id_for,
    mentioned_amount_with_unit,
    swap_direction_hints,
    unambiguous_amount,
    unambiguous_amount_with_unit,
)


def test_canonical_chain_maps_user_and_provider_ethereum_aliases():
    assert canonical_chain("Ethereum") == "ETH"
    assert canonical_chain("以太坊") == "ETH"
    assert canonical_chain("ETH") == "ETH"
    assert canonical_chain("ERC20") == "ETH"
    assert canonical_chain("Base") == "BASE"


def test_canonical_symbol_removes_provider_network_suffixes_and_typo_noise():
    assert canonical_symbol("USDT(ERC20)") == "USDT"
    assert canonical_symbol(" usd't ") == "USDT"
    assert canonical_symbol("usd-c") == "USDC"


def test_chain_id_for_uses_canonical_chain_name():
    assert chain_id_for("Ethereum") == 1
    assert chain_id_for("BASE") == 8453
    assert chain_id_for("unknown") is None


def test_canonical_amount_removes_a_single_token_unit_without_guessing():
    assert canonical_amount("1 USDC") == "1"
    assert canonical_amount(" 0.01ETH ") == "0.01"
    assert canonical_amount("大约 1 USDC") == "大约 1 USDC"
    assert unambiguous_amount("1 USDC") == "1"
    assert unambiguous_amount("0.01") == "0.01"
    assert unambiguous_amount("大约 1 USDC") is None


def test_unambiguous_amount_with_unit_preserves_token_semantics():
    assert unambiguous_amount_with_unit("5USDT") == ("5", "USDT")
    assert unambiguous_amount_with_unit("1 USDC") == ("1", "USDC")
    assert unambiguous_amount_with_unit("0.01") == ("0.01", None)
    assert unambiguous_amount_with_unit("大约 1 USDC") is None
    assert mentioned_amount_with_unit("我想换 5USDT") == ("5", "USDT")
    assert mentioned_amount_with_unit("在 Base 用 1 USDC 换 USDT") == ("1", "USDC")


def test_swap_direction_hints_understand_explicit_chinese_swap_grammar():
    assert swap_direction_hints("我要兑换一些usdt") == {"destination_symbol": "USDT"}
    assert swap_direction_hints("换一些以太上的 USDT") == {
        "destination_chain": "ETH",
        "destination_symbol": "USDT",
    }
    assert swap_direction_hints("用以太上的 usdc 换") == {
        "source_chain": "ETH",
        "source_symbol": "USDC",
    }
    assert swap_direction_hints("换成当前网络上的 USDT") == {"destination_symbol": "USDT"}
    assert swap_direction_hints("在 Base 用 1 USDC 换 USDT") == {}
    assert swap_direction_hints("用 Base 上的 USDC 换") == {
        "source_chain": "BASE",
        "source_symbol": "USDC",
    }
    assert swap_direction_hints("都在 Base 链") == {
        "source_chain": "BASE",
        "destination_chain": "BASE",
    }
    assert swap_direction_hints("来源也在 BSC 链") == {"source_chain": "BSC"}
    assert swap_direction_hints("目标也在 ETH 网络") == {"destination_chain": "ETH"}
    assert swap_direction_hints("以太上 10 USDC 换 BNB") == {
        "source_chain": "ETH",
        "source_symbol": "USDC",
        "destination_symbol": "BNB",
    }
