from __future__ import annotations

import httpx
import pytest

from app.hermes_runtime.activation import RuntimeActivator
from app.hermes_runtime.protocol import HermesProtocolError
from app.hermes_runtime.schemas import RuntimeEndpoint


class FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class ScriptedClient:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.calls: list[dict[str, object]] = []

    async def head(self, url: str, **kwargs):
        self.calls.append({"url": url, **kwargs})
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def endpoint() -> RuntimeEndpoint:
    return RuntimeEndpoint(
        runtime_workspace_id=7,
        runtime_id="rt-7",
        endpoint_base_url="http://127.0.0.1:30364",
        endpoint_host_header="runtime-10002.hermes-runtimes.hermes.local",
        api_key="runtime-key",
    )


def activator(client: ScriptedClient) -> RuntimeActivator:
    return RuntimeActivator(
        client=client,
        ready_timeout_seconds=1,
        poll_interval_seconds=0,
        max_poll_interval_seconds=0,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [200, 401, 404])
async def test_dashboard_route_response_completes_activation(status_code: int) -> None:
    client = ScriptedClient([FakeResponse(status_code)])

    await activator(client).activate_dashboard(endpoint())

    assert len(client.calls) == 1
    assert client.calls[0]["url"] == "http://127.0.0.1:30364/favicon.ico"
    assert client.calls[0]["headers"] == {
        "Host": "runtime-10002.hermes-runtimes.hermes.local"
    }
    assert client.calls[0]["timeout"] == pytest.approx(1.0, abs=0.01)


@pytest.mark.asyncio
async def test_activation_retries_dashboard_server_error_until_route_is_ready() -> None:
    client = ScriptedClient([FakeResponse(503), FakeResponse(404)])

    await activator(client).activate_dashboard(endpoint())

    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_activation_retries_dashboard_transport_error_until_route_is_ready() -> None:
    client = ScriptedClient(
        [
            httpx.ConnectError("route has no ready endpoint"),
            httpx.ConnectTimeout("activator is still starting"),
            FakeResponse(200),
        ]
    )

    await activator(client).activate_dashboard(endpoint())

    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_activation_stops_retrying_dashboard_at_deadline() -> None:
    class NeverReadyClient:
        def __init__(self) -> None:
            self.calls = 0

        async def head(self, _url: str, **_kwargs):
            self.calls += 1
            return FakeResponse(503)

    class StepClock:
        def __init__(self) -> None:
            self.value = -0.6

        def __call__(self) -> float:
            self.value += 0.6
            return self.value

    client = NeverReadyClient()
    runtime_activator = RuntimeActivator(
        client=client,
        ready_timeout_seconds=1,
        poll_interval_seconds=0,
        monotonic=StepClock(),
    )

    with pytest.raises(HermesProtocolError, match="activation timed out") as captured:
        await runtime_activator.activate_dashboard(endpoint())

    assert captured.value.status_code == 503
    assert captured.value.path == "/favicon.ico"
    assert client.calls == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ready_timeout_seconds": 0}, "ready_timeout_seconds"),
        ({"poll_interval_seconds": -1}, "poll_interval_seconds"),
        (
            {"poll_interval_seconds": 2, "max_poll_interval_seconds": 1},
            "max_poll_interval_seconds",
        ),
    ],
)
def test_activation_rejects_invalid_retry_configuration(
    kwargs: dict[str, float],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        RuntimeActivator(client=ScriptedClient([]), **kwargs)
