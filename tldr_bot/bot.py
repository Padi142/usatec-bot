import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .cats import GRID_LAYOUTS, CatClient, CatError, CatGenClient
from .config import Config
from .llm import LLMClient, LLMError, ModelInfo
from .signal_api import GroupDelete, GroupEdit, GroupMessage, SignalClient, parse_event, render_mentions
from .store import ChatMessage, MessageStore

log = logging.getLogger(__name__)

MACKA_RE = re.compile(r"^\s*/macka\s*$", re.IGNORECASE)
MACKA_GEN_RE = re.compile(r"^\s*/mackagen(?:\s+(.*?))?\s*$", re.IGNORECASE | re.DOTALL)

# CET/CEST. The tzdata package is a dependency because slim Docker images
# often ship without the system zoneinfo database.
LOCAL_TZ = ZoneInfo("Europe/Prague")

TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")

# Settings key for the model chosen with /model.
MODEL_SETTING = "llm_model"


@dataclass(frozen=True)
class TldrCommand:
    count: int | None = None  # None means "use the default"
    since: tuple[int, int] | None = None  # (hour, minute) in CET/CEST
    afk: bool = False  # summarize everything since the caller's last message
    model: str | None = None  # None means "use the current default"
    error: str | None = None


def _parse_count(args: list[str]) -> TldrCommand:
    if not args:
        return TldrCommand()
    if len(args) != 1 or not args[0].isdigit():
        return TldrCommand(error="usage")
    n = int(args[0])
    return TldrCommand(count=n) if n >= 1 else TldrCommand(error="usage")


def _parse_since(args: list[str]) -> TldrCommand:
    if len(args) != 1:
        return TldrCommand(error="usage")
    m = TIME_RE.match(args[0])
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        return TldrCommand(error="usage")
    return TldrCommand(since=(int(m[1]), int(m[2])))


def _parse_afk(args: list[str]) -> TldrCommand:
    return TldrCommand(afk=True) if not args else TldrCommand(error="usage")


# Subcommands of /tldr, keyed by their first argument word. To add a new
# form of the command, write a parser that turns the remaining words into a
# TldrCommand and register it here.
SUBCOMMANDS: dict[str, Callable[[list[str]], TldrCommand]] = {
    "since": _parse_since,
    "afk": _parse_afk,
}


def parse_command(text: str) -> TldrCommand | None:
    """Parse a /tldr command. Returns None if the text isn't a /tldr command.

    Recognized forms:
      /tldr              the default number of messages
      /tldr N            the last N messages
      /tldr since HH:MM  everything since that time (CET/CEST)
      /tldr afk          everything since the caller's last message

    Any form can end with a model slug, which is used for that summary only.
    """
    words = text.split()
    if not words or words[0].lower() != "/tldr":
        return None
    args = words[1:]
    parse = SUBCOMMANDS.get(args[0].lower()) if args else None
    rest = args[1:] if parse else args
    # Numbers and times are never slugs, so "/tldr 14:01" stays a usage error.
    model = None
    if rest and not rest[-1].isdigit() and not TIME_RE.match(rest[-1]):
        model, rest = rest[-1], rest[:-1]
    command = (parse or _parse_count)(rest)
    return command if command.error else replace(command, model=model)


def _now() -> datetime:
    """Current CET/CEST time; module-level so tests can freeze it."""
    return datetime.now(LOCAL_TZ)


def _epoch_ms(dt: datetime) -> int:
    return round(dt.timestamp() * 1000)


def resolve_since_time(hhmm: tuple[int, int], now: datetime) -> datetime:
    """The most recent occurrence of hh:mm, in the same timezone as `now`.

    If that time is still in the future today, this means yesterday at hh:mm.
    """
    hh, mm = hhmm
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target > now:
        target -= timedelta(days=1)
    return target


def format_since(cutoff: datetime, now: datetime) -> str:
    """Human label for a since-cutoff: HH:MM today, full date otherwise."""
    if cutoff.date() == now.date():
        return cutoff.strftime("%H:%M")
    return cutoff.strftime("%Y-%m-%d %H:%M")


def is_macka_command(text: str) -> bool:
    return MACKA_RE.match(text) is not None


@dataclass(frozen=True)
class MackaGenCommand:
    prompt: str | None = None
    layout: str | None = None  # a grid like "2x2"; None means a single image


def parse_macka_gen(text: str) -> MackaGenCommand | None:
    """Parse "/mackagen [2x2|3x3|4x4|5x5] [prompt]". Returns None for other commands."""
    m = MACKA_GEN_RE.match(text)
    if not m:
        return None
    arg = m.group(1) or ""
    first, *rest = arg.split(maxsplit=1) or [""]
    if first.lower() in GRID_LAYOUTS:
        return MackaGenCommand(prompt=rest[0] if rest else None, layout=first.lower())
    return MackaGenCommand(prompt=arg or None)


@dataclass(frozen=True)
class ModelCommand:
    slug: str | None = None  # None means "show the current model and the list"


def parse_model_command(text: str) -> ModelCommand | None:
    """Parse "/model [slug]". Returns None for other commands.

    Anything but a single argument shows the list, which also explains the usage.
    """
    words = text.split()
    if not words or words[0].lower() != "/model":
        return None
    return ModelCommand(slug=words[1] if len(words) == 2 else None)


def find_model(models: list[ModelInfo], slug: str) -> ModelInfo | None:
    """Look up a slug the way a phone keyboard might capitalize it."""
    return next((m for m in models if m.slug.lower() == slug.lower()), None)


def format_models(models: list[ModelInfo], current: str) -> str:
    """The /model listing: live models first, then down ones, then those without a status (mostly aliases)."""

    def line(model: ModelInfo) -> str:
        queue = f" ({model.waiting} queued)" if model.waiting else ""
        marker = "  <- current" if model.slug.lower() == current.lower() else ""
        return f"{'🟢' if model.online else '🔴'} {model.slug}{queue}{marker}"

    lines = [line(m) for m in models if m.online] + [line(m) for m in models if m.online is False]
    others = [m.slug for m in models if m.online is None]
    sections = ["\n".join(lines)] if lines else []
    if others:
        sections.append("No live status: " + ", ".join(others))
    return "\n\n".join(sections)


def build_transcript(messages: list[ChatMessage], max_chars: int) -> tuple[str, int]:
    """Format messages as transcript lines, keeping the newest ones that fit in max_chars.

    Returns the transcript and the number of messages included.
    """
    lines: list[str] = []
    total = 0
    for msg in reversed(messages):
        when = datetime.fromtimestamp(msg.timestamp / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
        line = f"[{when}] {msg.sender_name}: {msg.text}"
        if lines and total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    lines.reverse()
    return "\n".join(lines), len(lines)


class TldrBot:
    def __init__(
        self,
        cfg: Config,
        store: MessageStore,
        signal: SignalClient,
        llm: LLMClient,
        cats: CatClient,
        cat_gen: CatGenClient | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.signal = signal
        self.llm = llm
        self.cats = cats
        self.cat_gen = cat_gen
        self._tasks: set[asyncio.Task] = set()
        # Only one summary runs at a time, across all groups.
        self._busy = False

    async def run(self) -> None:
        async for payload in self.signal.events():
            try:
                self.handle(payload)
            except Exception:
                log.exception("Failed to handle payload: %.500s", payload)

    def handle(self, payload: dict) -> None:
        event = parse_event(payload)
        if event is None:
            return
        if self.cfg.allowed_groups and event.group_id not in self.cfg.allowed_groups:
            log.debug("Ignoring message from non-allowed group %s", event.group_id)
            return

        match event:
            case GroupDelete():
                self.store.delete(event.group_id, event.sender_id, event.target_timestamp)
            case GroupEdit():
                text = render_mentions(event.text, event.mentions, self.store.display_name)
                self.store.edit(event.group_id, event.sender_id, event.target_timestamp, text)
            case GroupMessage() if is_macka_command(event.text):
                log.info("/macka from %s in group %s", event.sender_name, event.group_id)
                self._spawn(self.run_macka(event))
            case GroupMessage() if (gen := parse_macka_gen(event.text)) is not None:
                log.info("/mackagen from %s in group %s", event.sender_name, event.group_id)
                self._spawn(self.run_macka_gen(event, gen))
            case GroupMessage() if (model_command := parse_model_command(event.text)) is not None:
                log.info("/model from %s in group %s", event.sender_name, event.group_id)
                self._spawn(self.run_model(event, model_command))
            case GroupMessage():
                command = parse_command(event.text)
                if command is None:
                    text = render_mentions(event.text, event.mentions, self.store.display_name)
                    self.store.add(
                        ChatMessage(event.group_id, event.sender_id, event.sender_name, event.timestamp, text)
                    )
                else:
                    log.info("/tldr from %s in group %s", event.sender_name, event.group_id)
                    self._spawn(self.run_tldr(event, command))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def reply(
        self, to: GroupMessage, text: str, attachments: list[tuple[str, bytes]] | None = None
    ) -> None:
        await self.signal.send_group(
            to.group_id,
            text,
            quote_timestamp=to.timestamp,
            quote_author=to.sender_number or to.sender_id,
            quote_text=to.text,
            attachments=attachments,
        )

    async def run_macka(self, msg: GroupMessage) -> None:
        try:
            try:
                cat = await self.cats.random_cat()
            except CatError as e:
                log.error("Cat download failed: %s", e)
                await self.reply(msg, "Sorry, I couldn't find a cat right now.")
                return
            await self.reply(msg, "🐈", attachments=[cat])
        except Exception:
            log.exception("/macka failed")

    async def run_macka_gen(self, msg: GroupMessage, command: MackaGenCommand) -> None:
        try:
            if self.cat_gen is None:
                await self.reply(msg, "Cat generation isn't set up (CAT_GEN_API_KEY is missing).")
                return
            if command.layout and command.prompt:
                await self.reply(
                    msg,
                    f"Grids only work without a prompt: /mackagen {command.layout}\n"
                    "With a prompt I can only make a single cat.",
                )
                return
            await self.signal.set_typing(msg.group_id, True)
            try:
                cat = await self.cat_gen.generate(command.prompt, command.layout)
            except CatError as e:
                log.error("Cat generation failed: %s", e)
                await self.reply(msg, "Sorry, I couldn't generate a cat right now.")
                return
            finally:
                await self.signal.set_typing(msg.group_id, False)
            await self.reply(msg, "🐈", attachments=[cat])
        except Exception:
            log.exception("/mackagen failed")

    @property
    def current_model(self) -> str:
        """The model /model chose, or the configured one."""
        return self.store.setting(MODEL_SETTING) or self.cfg.llm_model

    async def run_model(self, msg: GroupMessage, command: ModelCommand) -> None:
        try:
            await self._run_model(msg, command)
        except Exception:
            log.exception("/model failed")

    async def _run_model(self, msg: GroupMessage, command: ModelCommand) -> None:
        if command.slug is not None and command.slug.lower() == "default":
            self.store.set_setting(MODEL_SETTING, None)
            await self.reply(msg, f"Model reset to the default: {self.cfg.llm_model}")
            return

        try:
            models = await self.llm.available_models()
        except LLMError as e:
            log.error("Model list failed: %s", e)
            await self.reply(msg, f"Current model: {self.current_model}\nI couldn't fetch the model list right now.")
            return

        if command.slug is None:
            await self.reply(
                msg,
                f"Current model: {self.current_model}\n\n{format_models(models, self.current_model)}\n\n"
                "/model <slug> changes it, /model default resets it.\n"
                "/tldr <slug> uses a model for one summary.",
            )
            return

        model = find_model(models, command.slug)
        if model is None:
            await self.reply(msg, f"I don't know the model {command.slug}. /model lists the available ones.")
            return
        self.store.set_setting(MODEL_SETTING, model.slug)
        warning = "\nIt's down right now, so summaries may fail." if model.online is False else ""
        await self.reply(msg, f"Model changed to {model.slug}{warning}")

    async def run_tldr(self, msg: GroupMessage, command: TldrCommand) -> None:
        try:
            await self._run_tldr(msg, command)
        except Exception:
            log.exception("/tldr failed")

    async def _run_tldr(self, msg: GroupMessage, command: TldrCommand) -> None:
        if command.error:
            await self.reply(
                msg,
                "Usage: /tldr [N] | /tldr since HH:MM | /tldr afk\n"
                "Add a model slug at the end to use that model for one summary, like /tldr 50 glm.\n"
                f"Without a number I summarize the last {self.cfg.default_count} messages "
                f"(max {self.cfg.max_count}).\n"
                "since HH:MM summarizes everything since that time, in CET/CEST "
                "(a time later today means yesterday).\n"
                "afk summarizes everything since your last message.",
            )
            return

        # No await between the check and the assignment, so this can't race.
        if self._busy:
            await self.reply(msg, "I'm still working on another summary, try again once it's done.")
            return
        self._busy = True
        try:
            await self._summarize(msg, command)
        finally:
            self._busy = False

    async def _summarize(self, msg: GroupMessage, command: TldrCommand) -> None:
        model = self.current_model
        if command.model is not None:
            try:
                found = find_model(await self.llm.available_models(), command.model)
            except LLMError as e:
                log.error("Model list failed: %s", e)
                await self.reply(msg, f"I couldn't check the model {command.model} right now.")
                return
            if found is None:
                await self.reply(msg, f"I don't know the model {command.model}. /model lists the available ones.")
                return
            model = found.slug

        max_count = self.cfg.max_count
        capped = False
        since_label: str | None = None
        count = self.cfg.default_count

        if command.since is not None:
            now = _now()
            cutoff = resolve_since_time(command.since, now)
            since_label = format_since(cutoff, now)
            # One extra message, so we can tell whether the window overflowed.
            messages = self.store.last_before(
                msg.group_id, msg.timestamp, max_count + 1, after_timestamp=_epoch_ms(cutoff)
            )
        elif command.afk:
            anchor = self.store.last_from(msg.group_id, msg.sender_id, msg.timestamp)
            if anchor is None:
                await self.reply(msg, "I don't have any of your messages in this group.")
                return
            messages = self.store.last_before(
                msg.group_id, msg.timestamp, max_count + 1, after_timestamp=anchor.timestamp
            )
        else:
            requested = command.count or self.cfg.default_count
            count = min(requested, max_count)
            messages = self.store.last_before(msg.group_id, msg.timestamp, count)
            capped = requested > max_count

        if len(messages) > max_count:
            messages = messages[1:]  # keep only the newest max_count
            capped = True

        if not messages:
            if since_label is not None:
                await self.reply(msg, f"I don't have any messages since {since_label}.")
            elif command.afk:
                await self.reply(msg, "Nothing new since your last message.")
            else:
                await self.reply(
                    msg,
                    "I don't have any messages from this group yet. "
                    "I can only see messages sent after I was added.",
                )
            return

        transcript, included = build_transcript(messages, self.cfg.llm_max_input_chars)

        await self.signal.set_typing(msg.group_id, True)
        try:
            summary = await self.llm.summarize(transcript, model)
        except LLMError as e:
            log.error("LLM request failed: %s", e)
            await self.reply(msg, f"Sorry, I couldn't generate a summary with {model} right now.")
            return
        finally:
            await self.signal.set_typing(msg.group_id, False)

        if since_label is not None:
            header = f"TL;DR of {included} messages since {since_label}"
        elif command.afk:
            header = f"TL;DR of {included} messages since your last message"
        else:
            header = f"TL;DR of the last {included} messages"
        notes = []
        if command.model is not None:
            notes.append(f"using {model}")
        if capped:
            notes.append(f"capped at {max_count}")
        if since_label is None and not command.afk and len(messages) < count:
            notes.append(f"only {len(messages)} stored")
        if included < len(messages):
            notes.append("older ones trimmed to fit the LLM context")
        if notes:
            header += f" ({', '.join(notes)})"
        await self.reply(msg, f"{header}:\n\n{summary}")
