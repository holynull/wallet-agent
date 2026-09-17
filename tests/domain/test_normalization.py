from wallet_agent.domain.normalization import (
    canonical_amount,
    canonical_chain,
    canonical_symbol,
    chain_id_for,
    unambiguous_amount,
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
