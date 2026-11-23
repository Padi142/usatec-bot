"""Thin client for bbernhard/signal-cli-rest-api running in MODE=json-rpc."""

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import parse_qs, quote as quote_path, urlsplit
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
class Sticker:
    pack_id: str  # hex, as signal-cli and the sticker CDN write it
    sticker_id: int


@dataclass(frozen=True)
class Quote:
    """The message a reply quotes. Authors show up by UUID, number or both."""
    timestamp: int
    author_ids: tuple[str, ...]


@dataclass(frozen=True)
class GroupMessage:
    group_id: str
    sender_id: str
    sender_name: str
    sender_number: str | None
    timestamp: int
    text: str
    mentions: list[Mention] = field(default_factory=list)
    # What the sender typed, without the quote and attachment markers `text` has.
    body: str | None = None
    sticker: Sticker | None = None
    # The attachment id of the message's first image, for GET /v1/attachments/{id}.
    image: str | None = None
    quote: Quote | None = None


@dataclass(frozen=True)
class DirectMessage:
    sender_id: str
    sender_number: str | None
    text: str


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


Event = GroupMessage | GroupEdit | GroupDelete | DirectMessage


class AttachmentTooLarge(Exception):
    pass


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


def _sticker(data: dict) -> Sticker | None:
    sticker = data.get("sticker") or {}
    pack_id, sticker_id = sticker.get("packId"), sticker.get("stickerId")
    if isinstance(pack_id, str) and isinstance(sticker_id, int):
        return Sticker(pack_id.lower(), sticker_id)
    return None


def _image(data: dict) -> str | None:
    for attachment in data.get("attachments") or []:
        if str(attachment.get("contentType", "")).startswith("image/") and isinstance(attachment.get("id"), str):
            return attachment["id"]
    return None


def _quote(data: dict) -> Quote | None:
    quote = data.get("quote") or {}
    authors = tuple(a for a in (quote.get("authorUuid"), quote.get("authorNumber")) if isinstance(a, str) and a)
    if isinstance(quote.get("id"), int) and authors:
        return Quote(quote["id"], authors)
    return None


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


def _is_note_to_self(sent: dict, account: str | None) -> bool:
    return account is not None and account in (sent.get("destinationNumber"), sent.get("destination"))


def parse_event(payload: dict) -> Event | None:
    """Turn a raw websocket payload into an event we care about, or None."""
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
    is_sync = data is None
    if is_sync:
        # Messages sent from the account's primary device show up as sync messages
        # when the bot runs as a linked device.
        data = (env.get("syncMessage") or {}).get("sentMessage")
    if not data:
        return None

    group_id = (data.get("groupInfo") or {}).get("groupId")
    if not group_id:
        # On a linked account, synced direct messages are private chats with other
        # people. Only Note to Self is addressed to the bot.
        text = data.get("message")
        if not text or (is_sync and not _is_note_to_self(data, payload.get("account"))):
            return None
        return DirectMessage(sender_id, sender_number, text)

    if delete := data.get("remoteDelete"):
        return GroupDelete(group_id, sender_id, delete["timestamp"])

    text = _describe(data)
    if text is None:
        return None
    timestamp = data.get("timestamp") or env.get("timestamp")
    return GroupMessage(
        group_id, sender_id, sender_name, sender_number, timestamp, text, _mentions(data), data.get("message"),
        _sticker(data), _image(data), _quote(data),
    )


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
        sticker: Sticker | None = None,
    ) -> int | None:
        """Send a group message and return its timestamp. attachments are (content type, data) pairs.

        A sticker has to be from a pack the account has installed.
        """
        body: dict = {
            "number": self.number,
            "recipients": [to_rest_group_id(group_id)],
            "message": text,
        }
        if sticker:
            body["sticker"] = f"{sticker.pack_id}:{sticker.sticker_id}"
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
        # The REST API sends the timestamp as a decimal string.
        try:
            return int(resp.json()["timestamp"])
        except (ValueError, KeyError, TypeError):
            return None

    async def attachment(self, attachment_id: str, max_bytes: int) -> bytes:
        """A received attachment, which signal-cli downloads as messages arrive.

        Raises AttachmentTooLarge past max_bytes, without downloading the rest.
        """
        async with self._http.stream("GET", f"/v1/attachments/{quote_path(attachment_id, safe='')}") as resp:
            resp.raise_for_status()
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise AttachmentTooLarge(f"Attachment {attachment_id} is over {max_bytes} bytes")
            return bytes(data)

    async def install_sticker_pack(self, pack_id: str, pack_key: str) -> None:
        """Install a pack, so the account can send its stickers. signal-cli downloads it first."""
        resp = await self._http.post(
            f"/v1/sticker-packs/{self.number}", json={"pack_id": pack_id, "pack_key": pack_key}, timeout=180,
        )
        resp.raise_for_status()

    async def send_direct(self, recipient: str, text: str) -> None:
        """Send a direct message to a phone number or UUID."""
        resp = await self._http.post(
            "/v2/send", json={"number": self.number, "recipients": [recipient], "message": text},
        )
        resp.raise_for_status()

    async def send_reaction(self, group_id: str, emoji: str, target_author: str, timestamp: int) -> None:
        """React to the group message that target_author sent at timestamp."""
        resp = await self._http.post(
            f"/v1/reactions/{self.number}",
            json={
                "reaction": emoji,
                "recipient": to_rest_group_id(group_id),
                "target_author": target_author,
                "timestamp": timestamp,
            },
        )
        resp.raise_for_status()

    async def sticker_pack_key(self, pack_id: str) -> str | None:
        """The hex key of a sticker pack the account has seen, or None if it hasn't."""
        resp = await self._http.get(f"/v1/sticker-packs/{self.number}")
        resp.raise_for_status()
        for pack in resp.json():
            if str(pack.get("pack_id", "")).lower() == pack_id:
                # The pack's share link: https://signal.art/addstickers/#pack_id=...&pack_key=...
                keys = parse_qs(urlsplit(str(pack.get("url", ""))).fragment).get("pack_key")
                return keys[0] if keys else None
        return None

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
