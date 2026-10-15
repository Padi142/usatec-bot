import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .llm import LLMClient, LLMError
from .signal_api import GroupDelete, GroupEdit, GroupMessage, SignalClient, parse_event, render_mentions
from .store import ChatMessage, MessageStore

log = logging.getLogger(__name__)

COMMAND_RE = re.compile(r"^\s*/tldr(?:\s+(\S+))?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class TldrCommand:
    count: int | None = None  # None means "use the default"
    error: str | None = None


def parse_command(text: str) -> TldrCommand | None:
    """Parse "/tldr" or "/tldr N". Returns None if the text isn't a /tldr command."""
    m = COMMAND_RE.match(text)
    if not m:
        # "/tldr foo bar" is still meant as a command, just a malformed one.
        if text.strip().lower().startswith("/tldr "):
            return TldrCommand(error="usage")
        return None
    arg = m.group(1)
    if arg is None:
        return TldrCommand()
    if not arg.isdigit() or int(arg) < 1:
        return TldrCommand(error="usage")
    return TldrCommand(count=int(arg))


def build_transcript(messages: list[ChatMessage], max_chars: int) -> tuple[str, int]:
    """Format messages as transcript lines, keeping the newest ones that fit in max_chars.

    Returns the transcript and the number of messages included.
    """
    lines: list[str] = []
    total = 0
    for msg in reversed(messages):
        when = datetime.fromtimestamp(msg.timestamp / 1000).strftime("%Y-%m-%d %H:%M")
        line = f"[{when}] {msg.sender_name}: {msg.text}"
        if lines and total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    lines.reverse()
    return "\n".join(lines), len(lines)


class TldrBot:
    def __init__(self, cfg: Config, store: MessageStore, signal: SignalClient, llm: LLMClient):
        self.cfg = cfg
        self.store = store
        self.signal = signal
        self.llm = llm
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
            case GroupMessage():
                command = parse_command(event.text)
                if command is None:
                    text = render_mentions(event.text, event.mentions, self.store.display_name)
                    self.store.add(
                        ChatMessage(event.group_id, event.sender_id, event.sender_name, event.timestamp, text)
                    )
                else:
                    log.info("/tldr from %s in group %s", event.sender_name, event.group_id)
                    task = asyncio.create_task(self.run_tldr(event, command))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)

    async def reply(self, to: GroupMessage, text: str) -> None:
        await self.signal.send_group(
            to.group_id,
            text,
            quote_timestamp=to.timestamp,
            quote_author=to.sender_number or to.sender_id,
            quote_text=to.text,
        )

    async def run_tldr(self, msg: GroupMessage, command: TldrCommand) -> None:
        try:
            await self._run_tldr(msg, command)
        except Exception:
            log.exception("/tldr failed")

    async def _run_tldr(self, msg: GroupMessage, command: TldrCommand) -> None:
        if command.error:
            await self.reply(
                msg,
                f"Usage: /tldr [number of messages]\n"
                f"Without a number I summarize the last {self.cfg.default_count} messages "
                f"(max {self.cfg.max_count}).",
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
        requested = command.count or self.cfg.default_count
        count = min(requested, self.cfg.max_count)
        messages = self.store.last_before(msg.group_id, msg.timestamp, count)
        if not messages:
            await self.reply(
                msg,
                "I don't have any messages from this group yet. "
                "I can only see messages sent after I was added.",
            )
            return

        transcript, included = build_transcript(messages, self.cfg.llm_max_input_chars)

        await self.signal.set_typing(msg.group_id, True)
        try:
            summary = await self.llm.summarize(transcript)
        except LLMError as e:
            log.error("LLM request failed: %s", e)
            await self.reply(msg, "Sorry, I couldn't generate a summary right now.")
            return
        finally:
            await self.signal.set_typing(msg.group_id, False)

        header = f"TL;DR of the last {included} messages"
        notes = []
        if requested > self.cfg.max_count:
            notes.append(f"capped at {self.cfg.max_count}")
        if len(messages) < count:
            notes.append(f"only {len(messages)} stored")
        if included < len(messages):
            notes.append("older ones trimmed to fit the LLM context")
        if notes:
            header += f" ({', '.join(notes)})"
        await self.reply(msg, f"{header}:\n\n{summary}")
