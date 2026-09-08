from wallet_agent.graph.nodes import GraphRuntime


def test_graph_runtime_accepts_optional_price_provider():
    provider = object()
    runtime = GraphRuntime(model=object(), providers={}, chains={}, price_provider=provider)
    assert runtime.price_provider is provider
