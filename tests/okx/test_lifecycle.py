import pytest

from wallet_agent.api import create_app


@pytest.mark.asyncio
async def test_application_shutdown_closes_registered_transports():
    class Closable:
        def __init__(self):
            self.closed = False

        async def aclose(self):
            self.closed = True

    transport = Closable()
    app = create_app()
    app.state.transports.append(transport)

    async with app.router.lifespan_context(app):
        pass

    assert transport.closed is True
