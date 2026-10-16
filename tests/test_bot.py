import asyncio
import json
import os
from datetime import datetime, timedelta

import httpx
import pytest

from tldr_bot.bot import (
    LOCAL_TZ,
    TldrBot,
    TldrCommand,
    build_transcript,
    format_since,
    is_macka_command,
    parse_command,
    MackaGenCommand,
    parse_macka_gen,
    resolve_since_time,
)
from tldr_bot.cats import CatClient, CatError, CatGenClient
from tldr_bot.config import Config, normalize_group_id
from tldr_bot.health import heartbeat, is_healthy
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
        ("/tldr since 14:01", TldrCommand(since=(14, 1))),
        ("/tldr SINCE 9:05", TldrCommand(since=(9, 5))),
        ("/tldr  since  23:59  ", TldrCommand(since=(23, 59))),
        ("/tldr afk", TldrCommand(afk=True)),
        ("  /TLDR   AFK  ", TldrCommand(afk=True)),
        ("/tldr 0", TldrCommand(error="usage")),
        ("/tldr abc", TldrCommand(error="usage")),
        ("/tldr 5 more", TldrCommand(error="usage")),
        ("/tldr since", TldrCommand(error="usage")),
        ("/tldr since 25:00", TldrCommand(error="usage")),
        ("/tldr since 12:60", TldrCommand(error="usage")),
        ("/tldr since 1401", TldrCommand(error="usage")),
        ("/tldr since 14:01 pm", TldrCommand(error="usage")),
        ("/tldr since 14:01 15:02", TldrCommand(error="usage")),
        ("/tldr afk now", TldrCommand(error="usage")),
        ("/tldr 14:01", TldrCommand(error="usage")),
        ("hello", None),
        ("/tldrx", None),
        ("what does /tldr do?", None),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


def test_resolve_since_time_same_day():
    now = datetime(2026, 6, 5, 16, 23, 45, tzinfo=LOCAL_TZ)
    assert resolve_since_time((14, 1), now) == datetime(2026, 6, 5, 14, 1, tzinfo=LOCAL_TZ)


def test_resolve_since_time_previous_day():
    # The requested time is still in the future today, so it means yesterday.
    now = datetime(2026, 6, 5, 0, 24, tzinfo=LOCAL_TZ)
    assert resolve_since_time((23, 15), now) == datetime(2026, 6, 4, 23, 15, tzinfo=LOCAL_TZ)


def test_resolve_since_time_boundary():
    # hh:mm equal to the current wall clock is today (now still has seconds left).
    now = datetime(2026, 6, 5, 16, 23, 45, tzinfo=LOCAL_TZ)
    assert resolve_since_time((16, 23), now) == datetime(2026, 6, 5, 16, 23, tzinfo=LOCAL_TZ)


def test_format_since():
    now = datetime(2026, 6, 5, 16, 23, tzinfo=LOCAL_TZ)
    assert format_since(datetime(2026, 6, 5, 14, 1, tzinfo=LOCAL_TZ), now) == "14:01"
    assert format_since(datetime(2026, 6, 4, 23, 15, tzinfo=LOCAL_TZ), now) == "2026-06-04 23:15"


@pytest.mark.parametrize(
    "text, expected",
    [("/macka", True), ("  /MACKA ", True), ("/macka 3", False), ("/mackax", False), ("daj /macka", False)],
)
def test_is_macka_command(text, expected):
    assert is_macka_command(text) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/mackagen", MackaGenCommand()),
        ("  /MACKAGEN  ", MackaGenCommand()),
        ("/mackagen a cat in a hat", MackaGenCommand(prompt="a cat in a hat")),
        ("/mackagen  multi\nline ", MackaGenCommand(prompt="multi\nline")),
        ("/mackagen 2x2", MackaGenCommand(layout="2x2")),
        ("/mackagen 3X3 ", MackaGenCommand(layout="3x3")),
        ("/mackagen 2x2 a cat", MackaGenCommand(prompt="a cat", layout="2x2")),
        ("/mackagen 4x4", MackaGenCommand(prompt="4x4")),
        ("/macka", None),
        ("/mackagenx", None),
        ("hey /mackagen", None),
    ],
)
def test_parse_macka_gen(text, expected):
    assert parse_macka_gen(text) == expected


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


def epoch_ms(dt: datetime) -> int:
    return round(dt.timestamp() * 1000)


def test_store_last_before_and_dedup():
    store = MessageStore(":memory:", history_limit=100)
    for ts in range(1, 11):
        store.add(msg(ts, f"m{ts}"))
    store.add(msg(5, "duplicate"))
    store.add(msg(3, group="other"))
    got = store.last_before(GROUP, before_timestamp=9, limit=3)
    assert [m.text for m in got] == ["m6", "m7", "m8"]


def test_store_last_before_after_window():
    store = MessageStore(":memory:", history_limit=100)
    for ts in range(1, 11):
        store.add(msg(ts, f"m{ts}"))
    # Newest 3 of the messages in (4, 9).
    got = store.last_before(GROUP, before_timestamp=9, limit=3, after_timestamp=4)
    assert [m.text for m in got] == ["m6", "m7", "m8"]
    # No lower bound behaves as before.
    got = store.last_before(GROUP, 9, 100)
    assert [m.text for m in got] == [f"m{i}" for i in range(1, 9)]
    # An empty window yields no messages.
    assert store.last_before(GROUP, 100, 100, after_timestamp=100) == []


def test_store_last_from():
    store = MessageStore(":memory:", history_limit=100)
    store.add(msg(1, "a1", sender="alice"))
    store.add(msg(2, "b1", sender="bob"))
    store.add(msg(3, "a2", sender="alice"))
    assert store.last_from(GROUP, "alice", 10).text == "a2"
    assert store.last_from(GROUP, "alice", 3).text == "a1"  # strictly before
    assert store.last_from(GROUP, "alice", 1) is None
    assert store.last_from(GROUP, "carol", 10) is None


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


def test_build_transcript_uses_prague_time():
    ts = epoch_ms(datetime(2026, 6, 5, 14, 1, tzinfo=LOCAL_TZ))
    transcript, _ = build_transcript([msg(ts, "hello")], max_chars=1000)
    assert transcript.startswith("[2026-06-05 14:01] ")


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


def cat_gen_api(jobs: list, requests: list[httpx.Request], image=None):
    """A fake nano-catana API that serves the given job states in order."""
    states = iter(jobs)

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.host == "images":
            return image or httpx.Response(200, content=b"PNG", headers={"content-type": "image/png"})
        return httpx.Response(202 if request.method == "POST" else 200, json=next(states))

    cat_gen = CatGenClient("secret", base_url="http://catgen", poll_interval=0)
    cat_gen._http._transport = httpx.MockTransport(handler)
    return cat_gen


async def test_cat_gen_with_prompt():
    requests = []
    cat_gen = cat_gen_api(
        [
            {"id": "j1", "state": "queued", "images": [], "error": None},
            {"id": "j1", "state": "running", "images": [], "error": None},
            {"id": "j1", "state": "done", "images": [{"url": "http://images/j1/0.png"}], "error": None},
        ],
        requests,
    )
    assert await cat_gen.generate("a cat in a hat") == ("image/png", b"PNG")
    create, poll1, poll2, image = requests
    assert json.loads(create.content) == {"model": "flowmatch-cats-tti", "prompt": "a cat in a hat"}
    assert create.headers["authorization"] == "Bearer secret"
    assert poll2.url.path == "/api/v1/jobs/j1"
    # The image lives on another host, so the API key isn't sent there.
    assert "authorization" not in image.headers


async def test_cat_gen_without_prompt():
    requests = []
    cat_gen = cat_gen_api(
        [{"id": "j1", "state": "done", "images": [{"url": "http://images/j1/0.png"}], "error": None}], requests
    )
    await cat_gen.generate()
    assert json.loads(requests[0].content) == {"model": "cat-diffusion-v2"}


async def test_cat_gen_grid():
    requests = []
    cat_gen = cat_gen_api(
        [{"id": "j1", "state": "done", "images": [{"url": "http://images/j1/0.png"}], "error": None}], requests
    )
    await cat_gen.generate(layout="3x3")
    assert json.loads(requests[0].content) == {"model": "cat-diffusion-v2", "layout": "3x3"}


async def test_cat_gen_job_failed():
    cat_gen = cat_gen_api([{"id": "j1", "state": "failed", "images": [], "error": "GPU on fire"}], [])
    with pytest.raises(CatError, match="GPU on fire"):
        await cat_gen.generate()


async def test_cat_gen_rejects_non_image():
    done = {"id": "j1", "state": "done", "images": [{"url": "http://images/j1/0.png"}], "error": None}
    cat_gen = cat_gen_api([done], [], image=httpx.Response(200, text="<html>"))
    with pytest.raises(CatError, match="Expected an image"):
        await cat_gen.generate()


@pytest.mark.parametrize("job", [None, {"state": "queued"}])
async def test_cat_gen_malformed_job(job):
    cat_gen = cat_gen_api([job], [])
    with pytest.raises(CatError):
        await cat_gen.generate()


async def test_cat_gen_timeout():
    cat_gen = cat_gen_api([{"id": "j1", "state": "queued"}] * 1000, [])
    cat_gen.timeout = 0.05
    cat_gen.poll_interval = 0.01
    with pytest.raises(CatError, match="Timed out"):
        await cat_gen.generate()


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


class FakeCatGen:
    def __init__(self):
        self.calls = []

    async def generate(self, prompt=None, layout=None):
        self.calls.append((prompt, layout))
        return "image/png", b"PNG"


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
    return TldrBot(cfg, MessageStore(":memory:", 1000), FakeSignal(), FakeLLM(), FakeCats(), FakeCatGen())


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


# --- /tldr since -------------------------------------------------------------

NOW = datetime(2026, 6, 5, 16, 23, tzinfo=LOCAL_TZ)


def freeze_now(monkeypatch):
    monkeypatch.setattr("tldr_bot.bot._now", lambda: NOW)


async def test_tldr_since_same_day(monkeypatch):
    freeze_now(monkeypatch)
    bot = make_bot()
    for i, (h, m) in enumerate([(13, 0), (14, 0), (14, 1), (14, 2), (15, 30)]):
        ts = epoch_ms(datetime(2026, 6, 5, h, m, tzinfo=LOCAL_TZ))
        bot.handle(envelope(group_data(f"message {i}", ts=ts), name=f"P{i}", uuid=f"u{i}"))
    bot.handle(envelope(group_data("/tldr since 14:01", ts=epoch_ms(NOW)), name="P9", uuid="u9"))
    for task in list(bot._tasks):
        await task

    transcript = bot.llm.transcripts[0]
    assert "message 0" not in transcript  # 13:00, before the cutoff
    assert "message 1" not in transcript  # 14:00, before the cutoff
    assert "message 2" not in transcript  # 14:01 exactly; the boundary is exclusive
    assert "message 3" in transcript
    assert "message 4" in transcript
    [(group, text, _)] = bot.signal.sent
    assert group == GROUP
    assert text.startswith("TL;DR of 2 messages since 14:01:")


async def test_tldr_since_yesterday(monkeypatch):
    # The requested time is later today, so it means yesterday at that time.
    freeze_now(monkeypatch)
    bot = make_bot()
    for text_, dt in [
        ("before", datetime(2026, 6, 4, 22, 0, tzinfo=LOCAL_TZ)),
        ("after", datetime(2026, 6, 4, 23, 16, tzinfo=LOCAL_TZ)),
        ("today", datetime(2026, 6, 5, 0, 0, tzinfo=LOCAL_TZ)),
    ]:
        bot.handle(envelope(group_data(text_, ts=epoch_ms(dt)), name=f"P-{text_}", uuid=f"u-{text_}"))
    bot.handle(envelope(group_data("/tldr since 23:15", ts=epoch_ms(NOW)), name="P9", uuid="u9"))
    for task in list(bot._tasks):
        await task

    transcript = bot.llm.transcripts[0]
    assert "before" not in transcript
    assert "after" in transcript
    assert "today" in transcript
    [(_, text, _)] = bot.signal.sent
    assert text.startswith("TL;DR of 2 messages since 2026-06-04 23:15:")


async def test_tldr_since_capped_at_max_count(monkeypatch):
    freeze_now(monkeypatch)
    bot = make_bot(max_count=3)
    base = datetime(2026, 6, 5, 10, 0, tzinfo=LOCAL_TZ)
    for i in range(5):
        bot.handle(
            envelope(group_data(f"m{i}", ts=epoch_ms(base + timedelta(minutes=i))), name=f"P{i}", uuid=f"u{i}")
        )
    bot.handle(envelope(group_data("/tldr since 9:00", ts=epoch_ms(NOW)), name="P9", uuid="u9"))
    for task in list(bot._tasks):
        await task

    transcript = bot.llm.transcripts[0]
    assert "m0" not in transcript and "m1" not in transcript  # oldest ones dropped
    assert all(f"m{i}" in transcript for i in (2, 3, 4))
    [(_, text, _)] = bot.signal.sent
    assert text.startswith("TL;DR of 3 messages since 09:00 (capped at 3):")


async def test_tldr_since_empty_window(monkeypatch):
    freeze_now(monkeypatch)
    bot = make_bot()
    bot.handle(
        envelope(group_data("old", ts=epoch_ms(datetime(2026, 6, 5, 15, 0, tzinfo=LOCAL_TZ))), name="P0", uuid="u0")
    )
    bot.handle(envelope(group_data("/tldr since 16:00", ts=epoch_ms(NOW)), name="P1", uuid="u1"))
    for task in list(bot._tasks):
        await task
    [(_, text, _)] = bot.signal.sent
    assert text == "I don't have any messages since 16:00."


# --- /tldr afk ---------------------------------------------------------------

async def test_tldr_afk():
    bot = make_bot()
    for ts, name, uuid, text_ in [
        (1, "Alice", "u-alice", "question from alice"),
        (2, "Alice", "u-alice", "brb"),
        (3, "Bob", "u-bob", "answer 1"),
        (4, "Bob", "u-bob", "answer 2"),
    ]:
        bot.handle(envelope(group_data(text_, ts=ts), name=name, uuid=uuid))
    bot.handle(envelope(group_data("/tldr afk", ts=10), name="Alice", uuid="u-alice"))
    for task in list(bot._tasks):
        await task

    transcript = bot.llm.transcripts[0]
    assert "question from alice" not in transcript
    assert "brb" not in transcript  # her own last message is the anchor, not part of the summary
    assert "answer 1" in transcript
    assert "answer 2" in transcript
    [(_, text, _)] = bot.signal.sent
    assert text.startswith("TL;DR of 2 messages since your last message:")


async def test_tldr_afk_capped_at_max_count():
    bot = make_bot(max_count=3)
    bot.handle(envelope(group_data("mine", ts=1), name="Alice", uuid="u-alice"))
    for i in range(5):
        bot.handle(envelope(group_data(f"m{i}", ts=2 + i), name=f"P{i}", uuid=f"u{i}"))
    bot.handle(envelope(group_data("/tldr afk", ts=20), name="Alice", uuid="u-alice"))
    for task in list(bot._tasks):
        await task

    transcript = bot.llm.transcripts[0]
    assert "mine" not in transcript
    assert "m0" not in transcript and "m1" not in transcript
    assert all(f"m{i}" in transcript for i in (2, 3, 4))
    [(_, text, _)] = bot.signal.sent
    assert text.startswith("TL;DR of 3 messages since your last message (capped at 3):")


async def test_tldr_afk_nothing_new():
    bot = make_bot()
    bot.handle(envelope(group_data("brb", ts=1), name="Alice", uuid="u-alice"))
    bot.handle(envelope(group_data("/tldr afk", ts=10), name="Alice", uuid="u-alice"))
    for task in list(bot._tasks):
        await task
    [(_, text, _)] = bot.signal.sent
    assert text == "Nothing new since your last message."


async def test_tldr_afk_no_previous_message():
    bot = make_bot()
    bot.handle(envelope(group_data("hi", ts=1), name="Bob", uuid="u-bob"))
    bot.handle(envelope(group_data("/tldr afk", ts=10), name="Alice", uuid="u-alice"))
    for task in list(bot._tasks):
        await task
    [(_, text, _)] = bot.signal.sent
    assert "I don't have any of your messages" in text


async def test_usage_mentions_new_forms():
    bot = make_bot()
    bot.handle(envelope(group_data("/tldr xyz", ts=1)))
    for task in list(bot._tasks):
        await task
    [(_, text, _)] = bot.signal.sent
    assert "/tldr since HH:MM" in text
    assert "/tldr afk" in text


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


async def test_macka_gen():
    bot = make_bot()
    bot.handle(envelope(group_data("/mackagen a cat in a hat", ts=10)))
    bot.handle(envelope(group_data("/mackagen", ts=11)))
    bot.handle(envelope(group_data("/mackagen 2x2", ts=12)))
    for task in list(bot._tasks):
        await task
    assert bot.cat_gen.calls == [("a cat in a hat", None), (None, None), (None, "2x2")]
    assert [kwargs["attachments"] for _, _, kwargs in bot.signal.sent] == [[("image/png", b"PNG")]] * 3
    assert bot.store.last_before(GROUP, 10_000, 10) == []


async def test_macka_gen_grid_with_prompt():
    bot = make_bot()
    bot.handle(envelope(group_data("/mackagen 3x3 a cat in a hat", ts=10)))
    for task in list(bot._tasks):
        await task
    assert bot.cat_gen.calls == []
    [(_, text, _)] = bot.signal.sent
    assert "only work without a prompt" in text


async def test_macka_gen_not_configured():
    bot = make_bot()
    bot.cat_gen = None
    bot.handle(envelope(group_data("/mackagen", ts=10)))
    for task in list(bot._tasks):
        await task
    [(_, text, _)] = bot.signal.sent
    assert "CAT_GEN_API_KEY" in text


# --- healthcheck -------------------------------------------------------------

async def test_heartbeat(tmp_path):
    path = tmp_path / "healthy"
    signal = SignalClient("http://signal", "+420999")
    task = asyncio.create_task(heartbeat(signal, path, interval=0.01))
    await asyncio.sleep(0.05)
    assert not is_healthy(path)
    signal.connected = True
    await asyncio.sleep(0.05)
    assert is_healthy(path)
    signal.connected = False
    await asyncio.sleep(0.05)
    assert not is_healthy(path)
    task.cancel()


def test_stale_heartbeat(tmp_path):
    path = tmp_path / "healthy"
    path.touch()
    assert is_healthy(path)
    os.utime(path, (0, 0))
    assert not is_healthy(path)
