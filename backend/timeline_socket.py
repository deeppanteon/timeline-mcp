"""
Live push channel from Panteon Timeline (Socket.IO v2 over a WebSocket).

The Timeline web app keeps `wss://timeline.panteon.no/socket.io/?token="<jwt>"&EIO=3&transport=websocket`
open. This module listens on it with the same bearer token the REST client uses and
turns every pushed event into a callback, so the dashboard can check Timeline right
away instead of waiting for the next heartbeat. It carries no data into the database
itself: the REST sync stays the source of truth, the socket only says "look now".

The channel is not a documented API, so everything here is best effort: failures are
logged and retried with a growing pause, the polling scheduler keeps running either
way, and an expired token stops the listener instead of hammering the server.

Only the wire format we need is implemented (EIO 3 / Socket.IO 2): open, ping/pong,
namespace connect, event and error packets. Binary events are ignored.
"""
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import panteon_client

PING_FALLBACK_SECONDS = 25.0
BACKOFF_START_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0
STABLE_SECONDS = 60.0  # a connection that lasted this long resets the backoff
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
POLL_SECONDS = 1.0  # how often the receive loop looks at `stop` and the ping timer

# What the Help page shows: updated by the listener thread, read by the dashboard.
STATUS: Dict[str, Any] = {
    "enabled": False,
    "state": "off",  # off, connecting, connected, retrying, stopped
    "events": 0,
    "last_event": None,
    "last_event_at": None,
    "connected_since": None,
    "error": None,
}

_AUTH_WORDS = re.compile(r"auth|token|expired|denied|forbidden", re.I)
_EVENT_RE = re.compile(r"^(?:/[^,]*,)?(\d*)(\[.*)$", re.S)


def socket_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """TIMELINE_SOCKET=0/false/off/no turns the listener off; anything else leaves it on."""
    value = (env if env is not None else os.environ).get("TIMELINE_SOCKET", "")
    return value.strip().lower() not in ("0", "false", "off", "no")


def socket_url(token: str, base_url: Optional[str] = None) -> str:
    """The same URL the web app opens. The token goes in the query as a JSON string,
    exactly like the browser sends it; it is never logged (see `_redact`)."""
    parsed = urlparse(base_url or panteon_client.BASE_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    clean = token.replace("Bearer ", "").strip()
    return f"{scheme}://{parsed.netloc}/socket.io/?token={quote(json.dumps(clean), safe='')}&EIO=3&transport=websocket"


def parse_packet(text: str) -> Dict[str, Any]:
    """Decodes one text frame into {"type": open|ping|pong|connect|disconnect|event|error|other, …}.
    Events carry `event` (its name) and `args` (the rest of the array)."""
    if not text:
        return {"type": "other"}
    kind, rest = text[0], text[1:]
    if kind == "0":
        try:
            info = json.loads(rest)
        except ValueError:
            info = {}
        return {"type": "open", "ping_interval": float(info.get("pingInterval") or 0) / 1000 or PING_FALLBACK_SECONDS,
                "ping_timeout": float(info.get("pingTimeout") or 0) / 1000}
    if kind == "2":
        return {"type": "ping"}
    if kind == "3":
        return {"type": "pong"}
    if kind != "4" or not rest:
        return {"type": "other"}
    sio, body = rest[0], rest[1:]
    if sio == "0":
        return {"type": "connect"}
    if sio == "1":
        return {"type": "disconnect"}
    if sio == "4":
        return {"type": "error", "message": body}
    if sio == "2":
        match = _EVENT_RE.match(body)
        if not match:
            return {"type": "other"}
        try:
            items = json.loads(match.group(2))
        except ValueError:
            return {"type": "other"}
        if not isinstance(items, list) or not items or not isinstance(items[0], str):
            return {"type": "other"}
        return {"type": "event", "event": items[0], "args": items[1:]}
    return {"type": "other"}


def parse_event_filter(value: Optional[str]) -> Optional[set]:
    """TIMELINE_SOCKET_EVENTS: comma-separated event names to react to; empty or `*` = all."""
    names = {part.strip() for part in (value or "").split(",") if part.strip()}
    return None if not names or "*" in names else names


def _shape(value: Any) -> Any:
    """A payload reduced to its structure (keys and types), so the discovery log shows what
    Timeline pushes without copying task titles or comments to disk."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [_shape(value[0]), f"… {len(value)} items"] if len(value) > 1 else [_shape(v) for v in value]
    return type(value).__name__


def log_event(name: str, args: List[Any], full: bool = False) -> None:
    """Appends one JSON line to `<log dir>/timeline-socket-YYYY-MM-DD.jsonl` (event name and
    payload shape; the payload itself only when `full`, i.e. TIMELINE_SOCKET_LOG_PAYLOADS=1).
    Never raises."""
    try:
        now = datetime.now()
        entry = {"time": now.isoformat(timespec="milliseconds"), "event": name,
                 "payload": args if full else [_shape(a) for a in args]}
        log_dir = panteon_client.request_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / f"timeline-socket-{now.date().isoformat()}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _redact(text: str, token: str) -> str:
    clean = token.replace("Bearer ", "").strip()
    for secret in filter(None, (clean, quote(json.dumps(clean), safe=""))):
        text = text.replace(secret, "***")
    return text


class SocketRejected(Exception):
    """The server answered the connect with an authentication error packet."""


def _handshake_status(exc: Exception) -> Optional[int]:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def listen_once(
    url: str,
    on_event: Callable[[str, List[Any]], None],
    stop: threading.Event,
    connect: Optional[Callable[..., Any]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """One connection: returns when the server closes it or `stop` is set, raises on errors."""
    if connect is None:
        from websockets.sync.client import connect as ws_connect
        connect = ws_connect
    # the socket authenticates by the token in the URL; the browser's Origin and User-Agent go along
    with connect(url, origin=panteon_client.BASE_URL, open_timeout=15, close_timeout=2,
                 ping_interval=None, user_agent_header=USER_AGENT) as ws:
        ping_every, last_ping = PING_FALLBACK_SECONDS, clock()
        while not stop.is_set():
            try:
                frame = ws.recv(timeout=POLL_SECONDS)
            except TimeoutError:
                frame = None
            if clock() - last_ping >= ping_every:
                ws.send("2")  # Engine.IO 3: the client pings, the server answers 3
                last_ping = clock()
            if frame is None or isinstance(frame, bytes):
                continue
            packet = parse_packet(frame)
            kind = packet["type"]
            if kind == "open":
                ping_every = packet["ping_interval"]
                STATUS.update(state="connected", connected_since=datetime.now().isoformat(timespec="seconds"), error=None)
            elif kind == "ping":
                ws.send("3")
            elif kind == "error":
                message = packet["message"][:200]
                raise (SocketRejected if _AUTH_WORDS.search(message) else ConnectionError)(f"socket error packet: {message}")
            elif kind == "disconnect":
                return
            elif kind == "event":
                on_event(packet["event"], packet["args"])


def run_socket_listener(
    wake: threading.Event,
    stop: threading.Event,
    token_fn: Callable[[], str] = lambda: os.getenv("PANTEON_BEARER_TOKEN") or "",
    events: Optional[set] = None,
    log_payloads: bool = False,
    connect: Optional[Callable[..., Any]] = None,
    sleep: Optional[Callable[[float], bool]] = None,
) -> None:
    """Keeps the socket open until `stop` is set, setting `wake` for every event whose name is in
    `events` (None = all). Reconnects with a growing pause (5 s … 5 min); an expired or rejected
    token ends the listener, since retrying would only repeat the failure."""
    sleep = sleep or stop.wait
    token = token_fn()
    if not token:
        STATUS.update(enabled=False, state="off", error="no PANTEON_BEARER_TOKEN")
        return
    STATUS.update(enabled=True, state="connecting", error=None)
    url = socket_url(token)

    def on_event(name: str, args: List[Any]) -> None:
        STATUS.update(events=STATUS["events"] + 1, last_event=name, last_event_at=datetime.now().isoformat(timespec="seconds"))
        log_event(name, args, log_payloads)
        if events is None or name in events:
            wake.set()

    pause = BACKOFF_START_SECONDS
    while not stop.is_set():
        started = time.monotonic()
        try:
            STATUS.update(state="connecting")
            listen_once(url, on_event, stop, connect=connect)
            error = "closed by the server"
        except Exception as exc:
            status = _handshake_status(exc)
            error = _redact(f"{type(exc).__name__}: {exc}", token)
            if status in (401, 403) or isinstance(exc, SocketRejected):
                STATUS.update(state="stopped", connected_since=None, error=f"rejected ({status or 'token'}): refresh PANTEON_BEARER_TOKEN and restart")
                print(f"Socket listener stopped: {STATUS['error']}", flush=True)
                return
        if stop.is_set():
            break
        if time.monotonic() - started >= STABLE_SECONDS:
            pause = BACKOFF_START_SECONDS
        STATUS.update(state="retrying", connected_since=None, error=error)
        print(f"Socket listener: {error}; retrying in {int(pause)}s", flush=True)
        if sleep(pause):
            break
        pause = min(pause * 2, BACKOFF_MAX_SECONDS)
    STATUS.update(state="off", connected_since=None)
