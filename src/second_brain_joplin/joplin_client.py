"""Joplin Data API client (localhost:41184).

Thin async wrapper around the Joplin Web Clipper REST API. It returns raw API
dicts; shaping into response models and applying business rules (excerpts, day
filtering, who may write where) is the tool layer's job.

Requires Joplin Desktop running with the Web Clipper service enabled. The token
is available at Tools → Options → Web Clipper.

**When Joplin hangs.** A Joplin that was quit refuses the connection at once. One
that hangs accepts it and never answers, so each request would wait out the whole
timeout. After a request times out, the client asks Joplin's ``/ping``, with the
same timeouts as any request. No answer means Joplin isn't answering: for the
next ``HOLD_SECONDS`` every request is refused at once with the same message,
without being sent. When the hold ends, the next request pings first (the others
are still refused meanwhile): an answer lets it through, silence starts a new
hold. A Joplin that answers its ping is never held, however slow a request was.
"""

import time
from collections.abc import Callable
from enum import Enum
from typing import Any

import httpx

from .config import Settings
from .errors import (
    JoplinAPIError,
    JoplinAuthError,
    JoplinConnectionError,
    JoplinError,
    JoplinNotFoundError,
    JoplinUnavailableError,
)

JsonDict = dict[str, Any]
ItemTest = Callable[[JsonDict], bool]

# Failures that happen before a request is on the wire: no connection was made, or
# none was free. Anything else may have reached Joplin.
_NEVER_SENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
)

# The note properties a change may send Joplin (update_note).
CHANGEABLE = frozenset({"body", "title", "parent_id"})

# How long requests are refused at once after Joplin stopped answering.
HOLD_SECONDS = 30.0


class _Ping(Enum):
    ANSWERED = "answered"
    SILENT = "silent"  # connected, or tried to, and no answer in time
    REFUSED = "refused"  # nothing listening: Joplin quit, which fails fast anyway


class JoplinClient:
    """Async client over the Joplin Data API with typed error mapping."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # trust_env=False: Joplin is on this machine and its token travels in the
        # address of every request. httpx would otherwise obey HTTP_PROXY/ALL_PROXY
        # (and the system's proxy settings on macOS and Windows) and hand each
        # request, token included, to that proxy, which couldn't reach this
        # machine's Joplin anyway. It also stops httpx reading SSL_CERT_FILE and
        # SSL_CERT_DIR, which only an https Joplin behind a private CA would need.
        self._client = httpx.AsyncClient(
            base_url=settings.joplin_base_url,
            params={"token": settings.joplin_api_token},
            timeout=httpx.Timeout(settings.request_timeout, connect=settings.connect_timeout),
            trust_env=False,
        )
        # The hold (see the module's docstring). One client serves every tool call
        # and the background sync, so what one call learns, the next one knows.
        self.clock: Callable[[], float] = time.monotonic
        self._held_until: float | None = None
        self._checking = False  # a ping at the end of a hold is under way

    @property
    def base_url(self) -> str:
        return self._settings.joplin_base_url

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ping(self) -> bool:
        """Return True if the Joplin API is reachable and responding.

        Inside a hold it answers False at once, without asking.
        """
        try:
            response = await self._request("GET", "/ping")
        except JoplinError:
            return False
        return response.text.strip() == "JoplinClipperServer"

    # -- the hold --------------------------------------------------------------

    def _not_answering(self, *, maybe_sent: bool) -> JoplinUnavailableError:
        return JoplinUnavailableError(
            f"Joplin didn't answer at {self.base_url} within "
            f"{self._settings.request_timeout:g} seconds — is Joplin Desktop running "
            "and responsive?",
            maybe_sent=maybe_sent,
        )

    def _answered(self) -> None:
        """Joplin answered a request: whatever was held is over."""
        self._held_until = None

    def _hold(self) -> None:
        self._held_until = self.clock() + HOLD_SECONDS

    async def _ping_once(self) -> _Ping:
        """Ask ``/ping``, outside the hold, with the usual timeouts. Changes no state.

        A short timeout here would take a slow but healthy Joplin for a hung one,
        and hold it again at the end of every hold.
        """
        try:
            await self._client.get("/ping")
        except httpx.TimeoutException:
            return _Ping.SILENT
        except httpx.TransportError:
            return _Ping.REFUSED
        return _Ping.ANSWERED  # any answer at all, an error page included

    async def _gate(self) -> None:
        """Refuse at once inside a hold; at its end, let a request through only if Joplin pings."""
        if self._held_until is None:
            return
        if self._checking or self.clock() < self._held_until:
            raise self._not_answering(maybe_sent=False)
        self._checking = True
        try:
            ping = await self._ping_once()
        finally:  # a cancelled call must not leave the gate shut for good
            self._checking = False
        if ping is _Ping.SILENT:
            self._hold()
            raise self._not_answering(maybe_sent=False)
        # Joplin answers pings again (or quit, which the request will report itself).
        self._held_until = None

    async def _after_timeout(self) -> None:
        """A request timed out: hold if Joplin doesn't answer its ping either."""
        if await self._ping_once() is _Ping.SILENT:
            self._hold()

    # -- low-level helpers ---------------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        if status in (401, 403):
            raise JoplinAuthError(
                "Joplin rejected the request — check that JOPLIN_API_TOKEN is set and "
                "matches the token in Joplin's Web Clipper settings."
            )
        if status == 404:
            raise JoplinNotFoundError()
        raise JoplinAPIError(f"Joplin returned an unexpected response (HTTP {status}).")

    async def _request(
        self,
        method: str,
        path: str,
        params: JsonDict | None = None,
        json: JsonDict | None = None,
    ) -> httpx.Response:
        await self._gate()
        try:
            response = await self._client.request(method, path, params=params, json=json)
        except httpx.TimeoutException as exc:
            await self._after_timeout()
            raise self._not_answering(maybe_sent=not isinstance(exc, _NEVER_SENT)) from exc
        except httpx.TransportError as exc:  # refused, reset, DNS, protocol errors
            # Not a hang: whatever was held is over, and this fails fast by itself.
            self._answered()
            raise JoplinConnectionError(
                f"Joplin is unreachable at {self.base_url} — is Joplin Desktop running "
                "with the Web Clipper service enabled?",
                maybe_sent=not isinstance(exc, _NEVER_SENT),
            ) from exc
        self._answered()
        self._raise_for_status(response)
        return response

    async def _get(self, path: str, params: JsonDict | None = None) -> httpx.Response:
        return await self._request("GET", path, params=params)

    async def _get_paginated(
        self,
        path: str,
        params: JsonDict | None = None,
        *,
        max_items: int | None = None,
        keep: ItemTest | None = None,
        stop: ItemTest | None = None,
        max_pages: int | None = None,
    ) -> list[JsonDict]:
        """Follow Joplin's ``has_more``/``page`` pagination and collect items.

        ``keep`` drops the items it answers False for; only kept items count
        toward ``max_items``, so the walk goes on through further pages until
        that many are found or Joplin runs out. That is what lets a filtered
        listing still fill its limit. ``stop`` ends the walk at the first item it
        answers True for (that item is left out), for endpoints ordered so that
        nothing after it can qualify. ``max_pages`` ends the walk after that many
        pages whatever was found, so a filter that drops most items can't make a
        call page through the whole vault.
        """
        items: list[JsonDict] = []
        base_params = dict(params or {})
        page = 1
        while True:
            data = (await self._get(path, {**base_params, "page": page})).json()
            for item in data.get("items", []):
                if stop is not None and stop(item):
                    return items
                if keep is not None and not keep(item):
                    continue
                items.append(item)
                if max_items is not None and len(items) >= max_items:
                    return items
            if not data.get("has_more") or (max_pages is not None and page >= max_pages):
                return items
            page += 1

    # -- endpoints -----------------------------------------------------------

    async def get_folders(self) -> list[JsonDict]:
        """Return all notebooks (Joplin folders) with their hierarchy."""
        return await self._get_paginated("/folders", {"fields": "id,title,parent_id"})

    async def get_notes_index(self) -> list[JsonDict]:
        """Return every note's id and parent, for tallying notebook counts."""
        return await self._get_paginated("/notes", {"fields": "id,parent_id"})

    async def list_note_versions(self) -> list[JsonDict]:
        """Return every note's id, ``updated_time`` and notebook for incremental sync.

        Cheap (no bodies): used to detect which notes changed since the last
        embedding sync, and which the notebook filter lets through.
        """
        return await self._get_paginated("/notes", {"fields": "id,updated_time,parent_id"})

    async def get_note(self, note_id: str) -> JsonDict:
        """Return a single note including its full markdown body."""
        params = {
            "fields": "id,title,body,parent_id,created_time,updated_time,encryption_applied,"
            "is_todo,todo_due,todo_completed,deleted_time,is_conflict,markup_language"
        }
        data: JsonDict = (await self._get(f"/notes/{note_id}", params)).json()
        return data

    async def get_folder_notes(self, folder_id: str) -> list[JsonDict]:
        """Every note directly in one notebook (not its sub-notebooks), without bodies.

        Joplin's own listing leaves out notes in the trash. Conflict copies are
        flagged by ``is_conflict``, for the caller to leave out.
        """
        return await self._get_paginated(
            f"/folders/{folder_id}/notes",
            {"fields": "id,title,updated_time,is_todo,is_conflict,parent_id"},
        )

    async def get_note_tags(self, note_id: str) -> list[JsonDict]:
        """The tags on one note (id and title)."""
        return await self._get_paginated(f"/notes/{note_id}/tags", {"fields": "id,title"})

    async def get_note_resources(self, note_id: str) -> list[JsonDict]:
        """The attachments (resources) one note refers to, without their content."""
        return await self._get_paginated(
            f"/notes/{note_id}/resources", {"fields": "id,title,mime,size"}
        )

    async def get_resource_notes(self, resource_id: str) -> list[JsonDict]:
        """Every note that refers to an attachment, with its notebook."""
        return await self._get_paginated(
            f"/resources/{resource_id}/notes", {"fields": "id,parent_id"}
        )

    async def search(
        self, query: str, limit: int, keep: ItemTest | None = None, max_pages: int | None = None
    ) -> list[JsonDict]:
        """Keyword-search notes, returning the first ``limit`` hits ``keep`` lets through."""
        return await self._get_paginated(
            "/search",
            {"query": query, "type": "note", "fields": "id,title,body,parent_id,updated_time"},
            max_items=limit,
            keep=keep,
            max_pages=max_pages,
        )

    async def get_recent(
        self, limit: int, *, since_ms: int = 0, keep: ItemTest | None = None
    ) -> list[JsonDict]:
        """Return up to ``limit`` notes updated at or after ``since_ms``, newest first.

        The listing is ordered newest first, so the walk stops at the first note
        older than ``since_ms`` instead of paging through the rest of the vault.
        """
        return await self._get_paginated(
            "/notes",
            {
                "fields": "id,title,updated_time,parent_id",
                "order_by": "updated_time",
                "order_dir": "DESC",
            },
            max_items=limit,
            keep=keep,
            stop=lambda note: note.get("updated_time", 0) < since_ms,
        )

    async def note_exists(self, note_id: str) -> bool:
        """True if a note with this id exists (trash and conflicts aside)."""
        try:
            await self._get(f"/notes/{note_id}", {"fields": "id"})
        except JoplinNotFoundError:
            return False
        return True

    async def update_note(self, note_id: str, **change: str) -> JsonDict:
        """Change one property of a note, and only that one; return Joplin's answer.

        Joplin's ``PUT /notes/:id`` changes whatever properties it is sent, so the
        request carries exactly one, from a fixed list: ``body``, ``title`` or
        ``parent_id``. Returns an empty dict if the answer isn't a JSON object.
        """
        if len(change) != 1 or not set(change) <= CHANGEABLE:
            raise ValueError(f"one of {sorted(CHANGEABLE)} at a time, got {sorted(change)}")
        response = await self._request("PUT", f"/notes/{note_id}", json=dict(change))
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def create_note(self, note_id: str, title: str, body: str, parent_id: str) -> JsonDict:
        """Create a note with a chosen id in the notebook ``parent_id``; return Joplin's note.

        Returns an empty dict if Joplin's answer isn't a JSON object.

        The id is the caller's (32 hexadecimal characters) rather than one Joplin
        makes up, so the caller can tell afterwards whether a request that failed
        on the way back did create the note.
        """
        response = await self._request(
            "POST",
            "/notes",
            json={"id": note_id, "title": title, "body": body, "parent_id": parent_id},
        )
        # Joplin answered 2xx, so the note exists. An answer that isn't the note
        # as a JSON object must not turn that into a failure: the caller knows the id.
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
