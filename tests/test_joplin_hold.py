"""The hold: a Joplin that accepts connections and never answers costs one wait, not one per call.

Driven against the fake Joplin (respx), with a clock the test moves, so no test
waits in real time.
"""

import asyncio
from collections.abc import Iterator

import httpx
import pytest
import respx
from fakes import FakeJoplin

from second_brain_joplin.config import Settings
from second_brain_joplin.errors import (
    JoplinAPIError,
    JoplinConnectionError,
    JoplinUnavailableError,
)
from second_brain_joplin.joplin_client import HOLD_SECONDS, JoplinClient

MESSAGE = "didn't answer at http://localhost:41184 within 10 seconds"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def joplin() -> Iterator[FakeJoplin]:
    with respx.mock:
        fake = FakeJoplin()
        fake.install()
        fake.note("Plan", "apple", fake.notebook("Work"))
        yield fake


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(clock: Clock) -> JoplinClient:
    client = JoplinClient(Settings(joplin_api_token="tok"))
    client.clock = clock
    return client


async def _fails_with_the_message(client: JoplinClient) -> JoplinUnavailableError:
    with pytest.raises(JoplinUnavailableError, match=MESSAGE) as raised:
        await client.get_folders()
    return raised.value


async def test_a_hung_joplin_is_asked_once_then_refused_at_once(
    joplin: FakeJoplin, client: JoplinClient
) -> None:
    joplin.hung = True
    first = await _fails_with_the_message(client)
    assert first.maybe_sent is True  # a read timeout: it may have arrived
    assert (joplin.count("/folders"), joplin.pings) == (1, 1)

    for _ in range(5):
        held = await _fails_with_the_message(client)
        assert held.maybe_sent is False  # never sent
    assert (joplin.count("/folders"), joplin.pings) == (1, 1)  # nothing more was sent


async def test_one_slow_request_on_a_joplin_that_pings_holds_nothing(
    joplin: FakeJoplin, client: JoplinClient
) -> None:
    # Only the search is slow: /ping and the rest answer.
    route = respx.get(f"{joplin.base_url}/search").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(JoplinUnavailableError):
        await client.search("apple", limit=5)
    assert joplin.pings == 1

    assert await client.get_folders()  # not held: it was sent and answered
    assert joplin.count("/folders") == 1
    assert route.call_count == 1


async def test_an_answer_between_two_timeouts_resets_the_count(
    joplin: FakeJoplin, client: JoplinClient
) -> None:
    respx.get(f"{joplin.base_url}/search").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(JoplinUnavailableError):
        await client.search("apple", limit=5)
    await client.get_folders()  # Joplin answered
    with pytest.raises(JoplinUnavailableError):
        await client.search("apple", limit=5)

    assert joplin.pings == 2  # each timeout was a first one, and pinged
    assert await client.get_folders()  # and nothing is held


async def test_after_the_hold_a_ping_that_answers_lets_the_call_through(
    joplin: FakeJoplin, client: JoplinClient, clock: Clock
) -> None:
    joplin.hung = True
    await _fails_with_the_message(client)
    joplin.hung = False  # Joplin recovers

    clock.now += HOLD_SECONDS - 1
    await _fails_with_the_message(client)  # still held
    clock.now += 1
    assert await client.get_folders()
    assert (joplin.count("/folders"), joplin.pings) == (2, 2)
    assert await client.get_folders()  # cleared: no ping first
    assert joplin.pings == 2


async def test_after_the_hold_a_silent_ping_holds_again_without_sending_the_call(
    joplin: FakeJoplin, client: JoplinClient, clock: Clock
) -> None:
    joplin.hung = True
    await _fails_with_the_message(client)

    clock.now += HOLD_SECONDS
    await _fails_with_the_message(client)  # pinged, no answer: held again
    assert (joplin.count("/folders"), joplin.pings) == (1, 2)
    await _fails_with_the_message(client)
    assert joplin.pings == 2


async def test_only_one_call_pings_at_the_end_of_a_hold(
    joplin: FakeJoplin, client: JoplinClient, clock: Clock
) -> None:
    joplin.hung = True
    await _fails_with_the_message(client)
    clock.now += HOLD_SECONDS
    joplin.hung = False

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_ping(_request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        return httpx.Response(200, text="JoplinClipperServer")

    respx.get(f"{joplin.base_url}/ping").mock(side_effect=slow_ping)
    checking = asyncio.create_task(client.get_folders())
    await started.wait()
    await _fails_with_the_message(client)  # refused while the check is under way
    release.set()
    assert await checking
    assert await client.get_folders()


async def test_a_call_cancelled_during_the_check_leaves_no_stuck_gate(
    joplin: FakeJoplin, client: JoplinClient, clock: Clock
) -> None:
    joplin.hung = True
    await _fails_with_the_message(client)
    clock.now += HOLD_SECONDS
    joplin.hung = False

    started = asyncio.Event()

    async def never(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    route = respx.get(f"{joplin.base_url}/ping").mock(side_effect=never)
    task = asyncio.create_task(client.get_folders())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    route.mock(return_value=httpx.Response(200, text="JoplinClipperServer"))
    assert await client.get_folders()


async def test_an_error_answer_from_joplin_clears_the_hold_and_the_count(
    joplin: FakeJoplin, client: JoplinClient, clock: Clock
) -> None:
    joplin.hung = True
    await _fails_with_the_message(client)
    joplin.hung = False
    clock.now += HOLD_SECONDS
    route = respx.get(f"{joplin.base_url}/folders").mock(return_value=httpx.Response(500))
    with pytest.raises(JoplinAPIError):  # an answer, not a hang
        await client.get_folders()

    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(JoplinUnavailableError):
        await client.get_folders()
    assert joplin.pings == 3  # a first timeout again: it pinged, and Joplin answered
    route.mock(return_value=httpx.Response(200, json={"items": [], "has_more": False}))
    assert await client.get_folders() == []  # not held


async def test_a_refused_connection_is_not_a_hang_and_clears_the_hold(
    joplin: FakeJoplin, client: JoplinClient, clock: Clock
) -> None:
    joplin.hung = True
    await _fails_with_the_message(client)
    joplin.hung = False
    joplin.down = True  # Joplin was quit: nothing listens
    clock.now += HOLD_SECONDS

    with pytest.raises(JoplinConnectionError, match="unreachable") as raised:
        await client.get_folders()
    assert not isinstance(raised.value, JoplinUnavailableError)

    joplin.down = False
    clock.now -= HOLD_SECONDS
    assert await client.get_folders()


async def test_a_refusal_after_a_timeout_does_not_hold(
    joplin: FakeJoplin, client: JoplinClient
) -> None:
    """Joplin hung, then quit: the ping is refused, which fails fast by itself."""
    respx.get(f"{joplin.base_url}/search").mock(side_effect=httpx.ReadTimeout("slow"))
    joplin.down = True
    with pytest.raises(JoplinUnavailableError):
        await client.search("apple", limit=5)
    with pytest.raises(JoplinConnectionError, match="unreachable"):
        await client.get_folders()  # sent, not held
    assert joplin.count("/folders") == 1


async def test_ping_answers_false_at_once_inside_a_hold(
    joplin: FakeJoplin, client: JoplinClient
) -> None:
    joplin.hung = True
    await _fails_with_the_message(client)
    assert await client.ping() is False
    assert joplin.pings == 1  # the hold answered, not Joplin


async def test_a_ping_that_times_out_starts_a_hold(
    joplin: FakeJoplin, client: JoplinClient
) -> None:
    joplin.hung = True
    assert await client.ping() is False
    assert joplin.pings == 2  # the ping, then the check after its timeout
    await _fails_with_the_message(client)
    assert joplin.count("/folders") == 0


async def test_the_timeouts_are_the_settings() -> None:
    client = JoplinClient(Settings(request_timeout=7.5, connect_timeout=1.5))
    timeout = client._client.timeout
    assert (timeout.read, timeout.write, timeout.pool, timeout.connect) == (7.5, 7.5, 7.5, 1.5)


async def test_the_message_quotes_the_read_timeout(joplin: FakeJoplin) -> None:
    client = JoplinClient(Settings(joplin_api_token="tok", request_timeout=4))
    joplin.hung = True
    with pytest.raises(JoplinUnavailableError, match="within 4 seconds"):
        await client.get_folders()
    with pytest.raises(JoplinUnavailableError, match="within 4 seconds"):
        await client.get_folders()


async def test_timeouts_on_a_joplin_that_answers_its_ping_never_hold(
    joplin: FakeJoplin, client: JoplinClient
) -> None:
    """Two slow requests at once (a large search and the background sync, say)."""
    respx.get(f"{joplin.base_url}/search").mock(side_effect=httpx.ReadTimeout("slow"))
    calls = [client.search("apple", limit=5) for _ in range(2)]
    results = await asyncio.gather(*calls, return_exceptions=True)
    assert all(isinstance(r, JoplinUnavailableError) for r in results)
    assert joplin.pings == 2  # each timeout pinged, and Joplin answered

    assert await client.get_folders()  # not held
    assert joplin.count("/folders") == 1


async def test_a_joplin_that_pings_but_hangs_on_requests_is_asked_each_time(
    joplin: FakeJoplin, client: JoplinClient
) -> None:
    """Never held while it answers its ping: each request is sent and times out."""
    respx.get(f"{joplin.base_url}/folders").mock(side_effect=httpx.ReadTimeout("slow"))
    for _ in range(3):
        await _fails_with_the_message(client)
    assert joplin.pings == 3


async def test_the_ping_waits_as_long_as_a_request_may(joplin: FakeJoplin, clock: Clock) -> None:
    """A short ping would take a slow but healthy Joplin for a hung one, again and again."""
    client = JoplinClient(Settings(joplin_api_token="tok", request_timeout=7, connect_timeout=2))
    client.clock = clock
    seen: list[dict[str, float]] = []

    def ping(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, text="JoplinClipperServer")

    respx.get(f"{joplin.base_url}/ping").mock(side_effect=ping)
    respx.get(f"{joplin.base_url}/search").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(JoplinUnavailableError):
        await client.search("apple", limit=5)
    assert seen == [{"connect": 2, "read": 7, "write": 7, "pool": 7}]
