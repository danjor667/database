"""Local web GUI: a JSON API over the package, plus one static page.

    python -m app serve          # http://127.0.0.1:8000

## Why the standard library and not Flask

The same dependency policy as the CLI (see app/cli.py): the first framework
this project imports should exist for function, not convenience. The API below
is a dozen routes that parse a query string, call an existing `app` function,
and serialize the result — `http.server` covers that in fewer lines than a
framework's boilerplate would take. It is a *local development* GUI, served on
loopback by default; it has no authentication and must not be exposed further.

## Shape

* `GET /` serves `app/static/index.html` — the whole frontend, one file.
* `GET|POST /api/...` routes dispatch through the tables at the bottom.
* Domain errors map to HTTP the way the CLI maps them to exit codes:
  not-found → 404, "valid request, can't be done" → 409, bad input → 400,
  saturation → 503. Handlers never see a SQLSTATE, same as every other caller.
* One `Database` pool is shared by all request threads (it is thread-safe;
  that is its job). The Redis cache is optional exactly as in the CLI:
  constructed if redis is importable, degrading to uncached when down. The
  MongoDB store is opened per review request and its absence is a 503 for
  those routes only — the rest of the GUI works without it.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .analytics import (
    busiest_slots,
    find_nearby_available,
    nearby_restaurants,
    rank_restaurants_by_city,
    rating_trend,
)
from .availability import find_available_tables
from .cli import fetch_customers, fetch_restaurants
from .db import Database
from .errors import (
    BookingConflict,
    BookingError,
    CustomerNotFound,
    FeedbackNotFound,
    InvalidBookingRequest,
    NoTableAvailable,
    PoolExhausted,
    ReservationNotFound,
    RestaurantNotFound,
    ReviewNotFound,
    TableNotBookable,
)
from .reservations import book_reservation, cancel_reservation, list_customer_reservations

log = logging.getLogger(__name__)

_STATIC = Path(__file__).parent / "static"

# Domain exception -> HTTP status. Subclasses must precede BookingError.
_STATUS: list[tuple[type[BookingError], int]] = [
    (InvalidBookingRequest, 400),
    (RestaurantNotFound, 404),
    (CustomerNotFound, 404),
    (ReservationNotFound, 404),
    (ReviewNotFound, 404),
    (FeedbackNotFound, 404),
    (NoTableAvailable, 409),
    (TableNotBookable, 409),
    (BookingConflict, 409),
    (PoolExhausted, 503),
    (BookingError, 400),
]


class HttpError(Exception):
    """A client error the route itself detects (missing/malformed parameter)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# -- parameter helpers -------------------------------------------------------


def _param(params: Mapping[str, str], name: str, cast: Callable[[str], Any] = str) -> Any:
    if name not in params or params[name] == "":
        raise HttpError(400, f"missing parameter {name!r}")
    return _cast(params[name], name, cast)


def _opt(
    params: Mapping[str, Any], name: str, cast: Callable[[str], Any] = str, default: Any = None
) -> Any:
    value = params.get(name)
    if value is None or value == "":
        return default
    return _cast(value, name, cast)


def _cast(value: Any, name: str, cast: Callable[[str], Any]) -> Any:
    try:
        return cast(value)
    except (TypeError, ValueError):
        raise HttpError(400, f"parameter {name!r}: {value!r} is not a valid {cast.__name__}")


def _when(value: str) -> datetime:
    """ISO 8601; naive means UTC — the same convention as the CLI."""
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _open_store():
    from .feedback import FeedbackStore

    store = FeedbackStore.from_settings()
    if not store.is_available():
        store.close()
        raise HttpError(503, "MongoDB is not reachable — run ./scripts/db.sh up")
    return store


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


# -- routes ------------------------------------------------------------------
# Each takes (server, params) where params merges the query string with the
# JSON body for POSTs, and returns the payload to serialize.


def r_restaurants(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return fetch_restaurants(server.db)


def r_customers(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return fetch_customers(server.db, limit=_opt(params, "limit", int, 100))


def r_availability(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return find_available_tables(
        server.db,
        restaurant_id=_param(params, "restaurant_id", int),
        starts_at=_param(params, "at", _when),
        party_size=_param(params, "party", int),
        zone=_opt(params, "zone"),
        duration_minutes=_opt(params, "duration", int),
        cache=server.cache,
    )


def r_book(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return book_reservation(
        server.db,
        restaurant_id=_param(params, "restaurant_id", int),
        customer_id=_param(params, "customer_id", int),
        starts_at=_param(params, "at", _when),
        party_size=_param(params, "party", int),
        zone=_opt(params, "zone"),
        table_id=_opt(params, "table_id", int),
        duration_minutes=_opt(params, "duration", int),
        special_requests=_opt(params, "requests"),
        cache=server.cache,
    )


def r_cancel(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return cancel_reservation(
        server.db, _param(params, "reservation_id", int), cache=server.cache
    )


def r_reservations(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return list_customer_reservations(
        server.db,
        _param(params, "customer_id", int),
        limit=_opt(params, "limit", int, 20),
    )


def r_rank(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return rank_restaurants_by_city(
        server.db,
        city=_opt(params, "city"),
        max_rank=_opt(params, "top", int),
        min_reviews=_opt(params, "min_reviews", int, 0),
    )


def r_nearby(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return nearby_restaurants(
        server.db,
        longitude=_param(params, "lon", float),
        latitude=_param(params, "lat", float),
        radius_metres=_opt(params, "radius", float, 5000),
        min_rating=_opt(params, "min_rating", float),
        price_tier=_opt(params, "price_tier", int),
        limit=_opt(params, "limit", int, 20),
    )


def r_nearby_available(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return find_nearby_available(
        server.db,
        longitude=_param(params, "lon", float),
        latitude=_param(params, "lat", float),
        starts_at=_param(params, "at", _when),
        party_size=_param(params, "party", int),
        radius_metres=_opt(params, "radius", float, 5000),
        limit=_opt(params, "limit", int, 10),
    )


def r_slots(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return busiest_slots(
        server.db,
        restaurant_id=_opt(params, "restaurant_id", int),
        top_n=_opt(params, "top", int, 5),
    )


def r_trend(server: "AppServer", params: Mapping[str, Any]) -> Any:
    return rating_trend(server.db, restaurant_id=_opt(params, "restaurant_id", int))


def r_review_get(server: "AppServer", params: Mapping[str, Any]) -> Any:
    from .feedback import get_review_feedback

    store = _open_store()
    try:
        return get_review_feedback(server.db, store, _param(params, "reservation_id", int))
    finally:
        store.close()


def r_review_add(server: "AppServer", params: Mapping[str, Any]) -> Any:
    from .feedback import add_review

    store = _open_store()
    try:
        return add_review(
            server.db,
            store,
            reservation_id=_param(params, "reservation_id", int),
            rating=_param(params, "rating", int),
            body=_param(params, "body"),
            title=_opt(params, "title"),
            food_rating=_opt(params, "food", int),
            service_rating=_opt(params, "service", int),
            ambience_rating=_opt(params, "ambience", int),
            tags=params.get("tags") or (),
        )
    finally:
        store.close()


def r_review_search(server: "AppServer", params: Mapping[str, Any]) -> Any:
    store = _open_store()
    try:
        return store.search(
            _param(params, "q"),
            restaurant_id=_opt(params, "restaurant_id", int),
            limit=_opt(params, "limit", int, 20),
        )
    finally:
        store.close()


def r_reconcile(server: "AppServer", params: Mapping[str, Any]) -> Any:
    from .feedback import reconcile

    store = _open_store()
    try:
        return reconcile(server.db, store, repair=bool(params.get("repair")))
    finally:
        store.close()


GET_ROUTES: dict[str, Callable[["AppServer", Mapping[str, Any]], Any]] = {
    "/api/restaurants": r_restaurants,
    "/api/customers": r_customers,
    "/api/availability": r_availability,
    "/api/reservations": r_reservations,
    "/api/rank": r_rank,
    "/api/nearby": r_nearby,
    "/api/nearby-available": r_nearby_available,
    "/api/slots": r_slots,
    "/api/trend": r_trend,
    "/api/review": r_review_get,
    "/api/reviews/search": r_review_search,
}

POST_ROUTES: dict[str, Callable[["AppServer", Mapping[str, Any]], Any]] = {
    "/api/book": r_book,
    "/api/cancel": r_cancel,
    "/api/reviews": r_review_add,
    "/api/reconcile": r_reconcile,
}


# -- server ------------------------------------------------------------------


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], db: Database, cache: Any = None) -> None:
        super().__init__(address, Handler)
        self.db = db
        self.cache = cache


class Handler(BaseHTTPRequestHandler):
    server: AppServer  # narrowed from BaseServer for the route functions

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        log.debug("%s %s", self.address_string(), format % args)

    # -- plumbing --

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(_jsonable(payload), default=str).encode()
        self._send(status, body, "application/json")

    def _dispatch(
        self,
        routes: Mapping[str, Callable[["AppServer", Mapping[str, Any]], Any]],
        params: Mapping[str, Any],
    ) -> None:
        route = routes.get(urlsplit(self.path).path)
        if route is None:
            self._send_json(404, {"error": f"no such endpoint: {self.path}"})
            return
        try:
            self._send_json(200, route(self.server, params))
        except HttpError as exc:
            self._send_json(exc.status, {"error": str(exc)})
        except BookingError as exc:
            status = next(s for cls, s in _STATUS if isinstance(exc, cls))
            self._send_json(status, {"error": str(exc)})

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    # -- verbs --

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's contract
        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            try:
                page = (_STATIC / "index.html").read_bytes()
            except OSError:
                self._send_json(500, {"error": "app/static/index.html is missing"})
                return
            self._send(200, page, "text/html; charset=utf-8")
            return
        self._dispatch(GET_ROUTES, self._query())

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except ValueError as exc:
            self._send_json(400, {"error": f"invalid JSON body: {exc}"})
            return
        # Query-string parameters lose to body keys on collision; POSTs are
        # expected to carry everything in the body anyway.
        self._dispatch(POST_ROUTES, {**self._query(), **body})


def serve(
    db: Database,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
) -> int:
    """Run the GUI until interrupted. Returns a process exit code."""
    cache = None
    try:
        from .cache import AvailabilityCache

        cache = AvailabilityCache.from_settings()
    except ImportError:
        log.info("redis not installed; availability will not be cached")

    server = AppServer((host, port), db, cache)
    url = f"http://{host}:{server.server_address[1]}/"
    print(f"serving the reservation GUI on {url}  (Ctrl-C to stop)")
    if open_browser:
        # After serve_forever is already accepting, not before.
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
        if cache is not None:
            cache.close()
    return 0


__all__ = ["AppServer", "serve"]
