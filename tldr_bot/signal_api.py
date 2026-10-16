"""Thin client for bbernhard/signal-cli-rest-api running in MODE=json-rpc."""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import websockets

log = logging.getLogger(__name__)

MENTION_PLACEHOLDER = "￼"


@dataclass(frozen=True)
class Mention:
    start: int
    length: int
    uuid: str
    fallback: str


@dataclass(frozen=True)
class GroupMessage:
    group_id: str
    sender_id: str
    sender_name: str
    sender_number: str | None
    timestamp: int
    text: str
    mentions: list[Mention] = field(default_factory=list)


@dataclass(frozen=True)
class GroupEdit:
    group_id: str
    sender_id: str
    target_timestamp: int
    text: str
    mentions: list[Mention] = field(default_factory=list)


@dataclass(frozen=True)
class GroupDelete:
    group_id: str
    sender_id: str
    target_timestamp: int


Event = GroupMessage | GroupEdit | GroupDelete


def to_rest_group_id(internal_id: str) -> str:
    return "group." + base64.b64encode(internal_id.encode()).decode()


def _describe(data: dict) -> str | None:
    """Text content of a dataMessage, with placeholders for non-text content."""
    text = data.get("message")
    if not text:
        if data.get("sticker"):
            text = "[sticker]"
        elif data.get("attachments"):
            text = "[attachment]"
        else:
            return None  # reactions, receipts, group updates, ...
    elif data.get("attachments"):
        text = f"[attachment] {text}"

    quote = data.get("quote")
    if quote and quote.get("text"):
        snippet = " ".join(quote["text"].split())
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        text = f'(replying to "{snippet}") {text}'
    return text


def _mentions(data: dict) -> list[Mention]:
    return [
        Mention(
            start=m.get("start", 0),
            length=m.get("length", 1),
            uuid=m.get("uuid", ""),
            fallback=m.get("name") or m.get("number") or "someone",
        )
        for m in data.get("mentions") or []
    ]


def parse_event(payload: dict) -> Event | None:
    """Turn a raw websocket payload into a group event we care about, or None."""
    env = payload.get("envelope")
    if not env:
        return None

    sender_id = env.get("sourceUuid") or env.get("sourceNumber") or env.get("source")
    if not sender_id:
        return None
    sender_name = env.get("sourceName") or env.get("sourceNumber") or "Unknown"
    sender_number = env.get("sourceNumber")

    if edit := env.get("editMessage"):
        data = edit.get("dataMessage") or {}
        group_id = (data.get("groupInfo") or {}).get("groupId")
        text = _describe(data)
        if group_id and text and edit.get("targetSentTimestamp"):
            return GroupEdit(group_id, sender_id, edit["targetSentTimestamp"], text, _mentions(data))
        return None

    data = env.get("dataMessage")
    if data is None:
        # Messages sent from the account's primary device show up as sync messages
        # when the bot runs as a linked device.
        data = (env.get("syncMessage") or {}).get("sentMessage")
    if not data:
        return None

    group_id = (data.get("groupInfo") or {}).get("groupId")
    if not group_id:
        return None  # direct message

    if delete := data.get("remoteDelete"):
        return GroupDelete(group_id, sender_id, delete["timestamp"])

    text = _describe(data)
    if text is None:
        return None
    timestamp = data.get("timestamp") or env.get("timestamp")
    return GroupMessage(group_id, sender_id, sender_name, sender_number, timestamp, text, _mentions(data))


def render_mentions(text: str, mentions: list[Mention], name_for) -> str:
    """Replace Signal's mention placeholder characters with @Name."""
    for m in sorted(mentions, key=lambda m: m.start, reverse=True):
        if text[m.start : m.start + m.length] != MENTION_PLACEHOLDER * m.length:
            continue
        name = name_for(m.uuid) or m.fallback
        text = text[: m.start] + f"@{name}" + text[m.start + m.length :]
    return text


class SignalClient:
    def __init__(self, base_url: str, number: str):
        self.base_url = base_url
        self.number = number
        # True while the websocket for incoming messages is open.
        self.connected = False
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30)

    async def close(self) -> None:
        await self._http.aclose()

    async def events(self) -> AsyncIterator[dict]:
        """Yield incoming payloads forever, reconnecting on failure."""
        ws_url = self.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        ws_url = f"{ws_url}/v1/receive/{self.number}"
        backoff = 1
        while True:
            try:
                async with websockets.connect(ws_url, ping_interval=30, max_size=None) as ws:
                    log.info("Connected to %s", ws_url)
                    self.connected = True
                    backoff = 1
                    async for raw in ws:
                        try:
                            yield json.loads(raw)
                        except json.JSONDecodeError:
                            log.warning("Ignoring non-JSON frame: %.200s", raw)
            except (OSError, websockets.WebSocketException) as e:
                log.warning("Websocket disconnected (%s); reconnecting in %ss", e, backoff)
            finally:
                self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def send_group(
        self,
        group_id: str,
        text: str,
        quote_timestamp: int | None = None,
        quote_author: str | None = None,
        quote_text: str | None = None,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> None:
        """Send a group message. attachments are (content type, data) pairs."""
        body: dict = {
            "number": self.number,
            "recipients": [to_rest_group_id(group_id)],
            "message": text,
        }
        if attachments:
            body["base64_attachments"] = [
                f"data:{content_type};base64,{base64.b64encode(data).decode()}"
                for content_type, data in attachments
            ]
        if quote_timestamp and quote_author:
            body |= {
                "quote_timestamp": quote_timestamp,
                "quote_author": quote_author,
                "quote_message": quote_text or "",
            }
        resp = await self._http.post("/v2/send", json=body)
        resp.raise_for_status()

    async def set_typing(self, group_id: str, typing: bool) -> None:
        """Best-effort typing indicator; failures are only logged."""
        try:
            # httpx's .delete() doesn't take a body, hence .request().
            resp = await self._http.request(
                "PUT" if typing else "DELETE",
                f"/v1/typing-indicator/{self.number}",
                json={"recipient": to_rest_group_id(group_id)},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.debug("Typing indicator failed: %s", e)
