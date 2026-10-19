"""Chain metadata helpers: native decimals and chain logo URLs."""

from wallet_agent.domain.chains import (
    asset_logo_fields,
    chain_logo_url,
    native_decimals,
    token_logo_url,
)


def test_chain_logo_url_covers_supported_chains():
    assert chain_logo_url("ETH") == (
        "https://raw.githubusercontent.com/trustwallet/assets/master/"
        "blockchains/ethereum/info/logo.png"
    )
    assert chain_logo_url("BSC") == (
        "https://raw.githubusercontent.com/trustwallet/assets/master/"
        "blockchains/binance/info/logo.png"
    )
    assert chain_logo_url("TRON") == (
        "https://raw.githubusercontent.com/trustwallet/assets/master/"
        "blockchains/tron/info/logo.png"
    )
    assert chain_logo_url("BTC") == (
        "https://raw.githubusercontent.com/trustwallet/assets/master/"
        "blockchains/bitcoin/info/logo.png"
    )
    assert chain_logo_url("SOLANA") == (
        "https://raw.githubusercontent.com/trustwallet/assets/master/"
        "blockchains/solana/info/logo.png"
    )
    assert chain_logo_url("AVAXC") == (
        "https://raw.githubusercontent.com/trustwallet/assets/master/"
        "blockchains/avalanchec/info/logo.png"
    )
    assert chain_logo_url("trx") == chain_logo_url("TRON")
    assert chain_logo_url("UNKNOWN") is None
    assert chain_logo_url("") is None


def test_native_decimals_fallback_is_evm_18():
    assert native_decimals("ETH") == 18
    assert native_decimals("BASE") == 18
    assert native_decimals("TRON") == 6
    assert native_decimals("BTC") == 8
    assert native_decimals("SOLANA") == 9
    assert native_decimals("SOME_UNKNOWN") == 18


def test_chain_logo_url_covers_the_chains_the_design_lists():
    # The receive card names Avalanche / MegaETH next to the chains the App may
    # declare; both have a Trust Wallet entry (Avalanche's slug is avalanchec).
    assert chain_logo_url("AVALANCHE").endswith("/blockchains/avalanchec/info/logo.png")
    assert chain_logo_url("AVAX") == chain_logo_url("AVALANCHE")
    assert chain_logo_url("MEGAETH").endswith("/blockchains/megaeth/info/logo.png")
    assert chain_logo_url("CELO").endswith("/blockchains/celo/info/logo.png")
    # Robinhood has no Trust Wallet entry: a null logo (App draws a placeholder)
    # is correct, and must not be invented.
    assert chain_logo_url("ROBINHOOD") is None


def test_token_logo_url_only_resolves_natives_and_provider_logos():
    assert token_logo_url("BSC", "BNB") == chain_logo_url("BSC")
    assert token_logo_url("BSC", "bnb") == chain_logo_url("BSC")
    assert token_logo_url("ETH", "USDC") is None
    assert token_logo_url("ETH", "USDC", fallback="https://static.okx.com/usdc.png") == (
        "https://static.okx.com/usdc.png"
    )


def test_asset_logo_fields_pair_the_icon_with_its_chain_badge():
    fields = asset_logo_fields("BSC", "BNB")
    assert fields["logo_url"] == chain_logo_url("BSC")
    assert fields["chain_logo_url"] == chain_logo_url("BSC")

    # A provider logo wins over the native fallback.
    assert asset_logo_fields("BSC", "USDC", "https://static.okx.com/usdc.png") == {
        "logo_url": "https://static.okx.com/usdc.png",
        "chain_logo_url": chain_logo_url("BSC"),
    }

    # Without a chain there is nothing to look up.
    assert asset_logo_fields(None, "USDC") == {"logo_url": None, "chain_logo_url": None}
