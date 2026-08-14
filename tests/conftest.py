"""Shared fixtures.

Everything here is offline: a scripted model, in-memory adapters, and the
bundled simulated environment. No fixture reaches the network or a database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest

from aic.bootstrap import Container, build_container
from aic.config import LLMProvider, Settings
from aic.domain.models import Incident, Signal
from aic.guardrails.policy import ActionPolicy
from aic.llm.fake import ScriptedLLMClient
from aic.orchestration.state import InvestigationState
from aic.scenario import demo_incident, demo_signals


@pytest.fixture
def settings() -> Settings:
    return Settings(llm_provider=LLMProvider.FAKE, log_format="console")


@pytest.fixture
def container(settings: Settings) -> Container:
    """The real composition root, wired to in-memory adapters."""
    return build_container(settings)


@pytest.fixture
def policy() -> ActionPolicy:
    """The default policy: nothing above read-only is auto-approved."""
    return ActionPolicy()


@pytest.fixture
def scripted_llm() -> ScriptedLLMClient:
    """A bare scripted client. Register the handlers each test needs."""
    return ScriptedLLMClient()


@pytest.fixture
def incident() -> Incident:
    return demo_incident()


@pytest.fixture
def signals() -> list[Signal]:
    return demo_signals()


@pytest.fixture
def state(incident: Incident, signals: list[Signal]) -> InvestigationState:
    """A fresh investigation over the bundled scenario."""
    return InvestigationState(run_id="test-run", incident=incident, signals=signals)


@pytest.fixture
async def api_client(container: Container) -> AsyncIterator[object]:
    """An httpx client bound to the real app with the offline container."""
    import httpx

    from aic.api.app import create_app

    app = create_app(container.settings, container=container)
    transport = httpx.ASGITransport(app=app)
    # The lifespan is what installs app.state.container.
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        yield client


@pytest.fixture(autouse=True)
def _quiet_logging() -> Iterator[None]:
    """Keep test output readable; log configuration is exercised separately."""
    import logging

    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)
