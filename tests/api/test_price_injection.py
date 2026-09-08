from wallet_agent.api import create_app


def test_create_app_accepts_optional_price_provider():
    provider = object()
    app = create_app(price_provider=provider)
    assert app.state.price_provider is provider
