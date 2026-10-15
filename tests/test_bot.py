import asyncio
import json

import httpx
import pytest

from tldr_bot.bot import TldrBot, TldrCommand, build_transcript, is_macka_command, parse_command
from tldr_bot.cats import CatClient, CatError
from tldr_bot.config import Config, normalize_group_id
from tldr_bot.llm import LLMClient, LLMError
from tldr_bot.signal_api import (
    GroupDelete,
    GroupEdit,
    GroupMessage,
    Mention,
    SignalClient,
    parse_event,
    render_mentions,
    to_rest_group_id,
)
from tldr_bot.store import ChatMessage, MessageStore

GROUP = "abcDEF123+/="


def envelope(data=None, *, sync=False, edit=None, name="Alice", uuid="uuid-alice", ts=1000):
    env = {"sourceUuid": uuid, "sourceName": name, "sourceNumber": "+420111", "timestamp": ts}
    if edit:
        env["editMessage"] = edit
    elif sync:
        env["syncMessage"] = {"sentMessage": data}
    else:
        env["dataMessage"] = data
    return {"envelope": env, "account": "+420999"}


def group_data(message="hi", ts=1000, **extra):
    return {"timestamp": ts, "message": message, "groupInfo": {"groupId": GROUP, "type": "DELIVER"}, **extra}


# --- command parsing ---------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("/tldr", TldrCommand()),
        ("/tldr 50", TldrCommand(count=50)),
        ("  /TLDR   7 ", TldrCommand(count=7)),
        ("/tldr 0", TldrCommand(error="usage")),
        ("/tldr abc", TldrCommand(error="usage")),
        ("/tldr 5 more", TldrCommand(error="usage")),
        ("hello", None),
        ("/tldrx", None),
        ("what does /tldr do?", None),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [("/macka", True), ("  /MACKA ", True), ("/macka 3", False), ("/mackax", False), ("daj /macka", False)],
)
def test_is_macka_command(text, expected):
    assert is_macka_command(text) is expected


# --- envelope parsing --------------------------------------------------------

def test_parse_group_message():
    ev = parse_event(envelope(group_data("hello", ts=42)))
    assert ev == GroupMessage(GROUP, "uuid-alice", "Alice", "+420111", 42, "hello", [])


def test_parse_sync_message():
    ev = parse_event(envelope(group_data("from my phone"), sync=True))
    assert isinstance(ev, GroupMessage) and ev.text == "from my phone"


def test_ignores_direct_messages_and_reactions():
    assert parse_event(envelope({"timestamp": 1, "message": "dm"})) is None
    assert parse_event(envelope(group_data(None, reaction={"emoji": "👍"}))) is None
    assert parse_event({"envelope": {"sourceUuid": "x", "receiptMessage": {}}}) is None


def test_attachment_and_quote():
    ev = parse_event(envelope(group_data(None, attachments=[{}])))
    assert ev.text == "[attachment]"
    ev = parse_event(envelope(group_data("yes", quote={"text": "pizza tonight?"})))
    assert ev.text == '(replying to "pizza tonight?") yes'


def test_parse_edit_and_delete():
    ev = parse_event(envelope(edit={"targetSentTimestamp": 5, "dataMessage": group_data("fixed")}))
    assert ev == GroupEdit(GROUP, "uuid-alice", 5, "fixed", [])
    ev = parse_event(envelope(group_data(None, remoteDelete={"timestamp": 5})))
    assert ev == GroupDelete(GROUP, "uuid-alice", 5)


def test_render_mentions():
    text = "hi ￼ and ￼!"
    mentions = [Mention(3, 1, "u1", "+1"), Mention(9, 1, "u2", "+2")]
    names = {"u1": "Bob"}
    assert render_mentions(text, mentions, names.get) == "hi @Bob and @+2!"


def test_group_id_roundtrip():
    assert normalize_group_id(to_rest_group_id(GROUP)) == GROUP
    assert normalize_group_id(GROUP) == GROUP


# --- storage -----------------------------------------------------------------

def msg(ts, text="m", group=GROUP, sender="a"):
    return ChatMessage(group, sender, sender.upper(), ts, text)


def test_store_last_before_and_dedup():
    store = MessageStore(":memory:", history_limit=100)
    for ts in range(1, 11):
        store.add(msg(ts, f"m{ts}"))
    store.add(msg(5, "duplicate"))
    store.add(msg(3, group="other"))
    got = store.last_before(GROUP, before_timestamp=9, limit=3)
    assert [m.text for m in got] == ["m6", "m7", "m8"]


def test_store_edit_delete_prune():
    store = MessageStore(":memory:", history_limit=3)
    for ts in range(1, 6):
        store.add(msg(ts, f"m{ts}"))
    store.edit(GROUP, "a", 4, "edited")
    store.delete(GROUP, "a", 5)
    store.prune()
    assert [m.text for m in store.last_before(GROUP, 100, 100)] == ["m2", "m3", "edited"]


def test_build_transcript_trims_oldest():
    messages = [msg(i * 60_000, "x" * 50) for i in range(10)]
    transcript, n = build_transcript(messages, max_chars=250)
    assert n == 3
    assert transcript.count("\n") == 2
    # Always include at least one message, even if it's over budget.
    _, n = build_transcript(messages, max_chars=1)
    assert n == 1


# --- LLM client --------------------------------------------------------------

async def test_llm_summarize_strips_think():
    def handler(request: httpx.Request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer sk-test"
        assert "<transcript>" in body["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>hmm</think>\nSummary"}}]})

    llm = LLMClient("http://llm/v1", "m", "sk-test", 10)
    llm._http._transport = httpx.MockTransport(handler)
    assert await llm.summarize("[t] A: hi") == "Summary"


async def test_llm_error():
    llm = LLMClient("http://llm/v1", "m", None, 10)
    llm._http._transport = httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(LLMError, match="HTTP 500"):
        await llm.summarize("x")


# --- cat client --------------------------------------------------------------

async def test_random_cat():
    cats = CatClient("http://cats/macka")
    cats._http._transport = httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"JPEG", headers={"content-type": "image/jpeg"})
    )
    assert await cats.random_cat() == ("image/jpeg", b"JPEG")


async def test_random_cat_rejects_non_image():
    cats = CatClient("http://cats/macka")
    cats._http._transport = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(CatError, match="Expected an image"):
        await cats.random_cat()


async def test_send_group_with_attachment():
    bodies = []

    def handler(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={})

    signal = SignalClient("http://signal", "+420999")
    signal._http._transport = httpx.MockTransport(handler)
    await signal.send_group(GROUP, "cat", attachments=[("image/jpeg", b"JPEG")])
    assert bodies[0]["base64_attachments"] == ["data:image/jpeg;base64,SlBFRw=="]


# --- end to end through the bot ----------------------------------------------

class FakeSignal:
    def __init__(self):
        self.sent = []

    async def send_group(self, group_id, text, **kwargs):
        self.sent.append((group_id, text, kwargs))

    async def set_typing(self, group_id, typing):
        pass


class FakeCats:
    def __init__(self, error=None):
        self.error = error

    async def random_cat(self):
        if self.error:
            raise self.error
        return "image/jpeg", b"JPEG"


class FakeLLM:
    def __init__(self):
        self.transcripts = []

    async def summarize(self, transcript):
        self.transcripts.append(transcript)
        return "they talked"


def make_bot(**overrides):
    settings = dict(
        signal_api_url="http://x", signal_number="+420999", llm_base_url="http://llm", llm_model="m",
        llm_api_key=None, llm_timeout=10, llm_max_input_chars=10_000, db_path=":memory:",
        default_count=50, max_count=100, history_limit=1000,
        allowed_groups=frozenset(),
    )
    cfg = Config(**(settings | overrides))
    return TldrBot(cfg, MessageStore(":memory:", 1000), FakeSignal(), FakeLLM(), FakeCats())


async def test_tldr_end_to_end():
    bot = make_bot()
    for i in range(1, 6):
        bot.handle(envelope(group_data(f"message {i}", ts=i), name=f"P{i}", uuid=f"u{i}"))
    bot.handle(envelope(group_data("/tldr 3", ts=10)))
    for task in list(bot._tasks):
        await task

    assert len(bot.llm.transcripts) == 1
    transcript = bot.llm.transcripts[0]
    assert "message 2" not in transcript
    assert all(f"P{i}: message {i}" in transcript for i in (3, 4, 5))
    assert "/tldr" not in transcript

    [(group, text, quote)] = bot.signal.sent
    assert group == GROUP
    assert text.startswith("TL;DR of the last 3 messages:")
    assert quote["quote_timestamp"] == 10


async def test_tldr_empty_history():
    bot = make_bot()
    bot.handle(envelope(group_data("/tldr", ts=10)))
    for task in list(bot._tasks):
        await task
    [(_, text, _)] = bot.signal.sent
    assert "don't have any messages" in text


async def test_allowed_groups_filter():
    bot = make_bot(allowed_groups=frozenset({"other"}))
    bot.handle(envelope(group_data("hello")))
    assert bot.store.last_before(GROUP, 10_000, 10) == []


async def test_only_one_tldr_at_a_time():
    bot = make_bot()
    release = asyncio.Event()

    async def slow_summarize(transcript):
        await release.wait()
        return "done"

    bot.llm.summarize = slow_summarize
    bot.handle(envelope(group_data("hello", ts=1)))
    bot.handle(envelope(group_data("/tldr", ts=10)))
    await asyncio.sleep(0)  # let the first request start
    bot.handle(envelope(group_data("/tldr", ts=11)))
    bot.handle(envelope(group_data("/tldr abc", ts=12)))  # usage help isn't blocked
    await asyncio.sleep(0)

    texts = [t for _, t, _ in bot.signal.sent]
    assert any("still working" in t for t in texts)
    assert any(t.startswith("Usage:") for t in texts)

    release.set()
    for task in list(bot._tasks):
        await task
    assert bot.signal.sent[-1][1].startswith("TL;DR")

    # Once the first one is done, new requests go through again.
    bot.handle(envelope(group_data("/tldr", ts=20)))
    for task in list(bot._tasks):
        await task
    assert bot.signal.sent[-1][1].startswith("TL;DR")


async def test_macka():
    bot = make_bot()
    bot.handle(envelope(group_data("/macka", ts=10)))
    for task in list(bot._tasks):
        await task
    [(group, _, kwargs)] = bot.signal.sent
    assert group == GROUP
    assert kwargs["attachments"] == [("image/jpeg", b"JPEG")]
    assert kwargs["quote_timestamp"] == 10
    # Commands aren't part of the chat history.
    assert bot.store.last_before(GROUP, 10_000, 10) == []


async def test_macka_download_failure():
    bot = make_bot()
    bot.cats = FakeCats(error=CatError("down"))
    bot.handle(envelope(group_data("/macka", ts=10)))
    for task in list(bot._tasks):
        await task
    [(_, text, kwargs)] = bot.signal.sent
    assert "couldn't find a cat" in text
    assert kwargs["attachments"] is None
