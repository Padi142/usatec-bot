"""Emoji reactions: the editable rules, the /react command and the decision model's question."""

import json
import unicodedata
from dataclasses import asdict, dataclass

from .llm import Choice

MAX_RULES = 20
MAX_TOPIC_CHARS = 200
# Only the start of a long message is judged, which is plenty to find its topic.
MAX_MESSAGE_CHARS = 2000

# Most messages should get no reaction, so the label for them can't be a rule's emoji.
NO_REACTION = "none"
INSTRUCTIONS = "Which topic is this chat message about? Most messages are about none of them."
# Correct matches scored at least 0.68 on a Czech and English test set, so below this it's a guess.
MIN_CONFIDENCE = 0.6

_VARIATION_SELECTOR = "️"
_EMOJI_CATEGORIES = {"So", "Sk", "Mn", "Cf"}  # symbols, skin tones, variation selectors, joiners


@dataclass(frozen=True)
class ReactionRule:
    emoji: str
    topic: str  # what a message has to be about, as the decision model reads it


DEFAULT_RULES = (
    ReactionRule("🐈", "cats"),
    ReactionRule("🐦", "birds"),
    ReactionRule("🍺", "beer"),
    ReactionRule("🚲", "bicycles and cycling"),
    ReactionRule("🛏️", "sleep, being tired, going to bed"),
)


def load_rules(raw: str | None) -> tuple[ReactionRule, ...]:
    """Rules from their stored JSON. None means they were never edited."""
    if raw is None:
        return DEFAULT_RULES
    return tuple(ReactionRule(**rule) for rule in json.loads(raw))


def dump_rules(rules: tuple[ReactionRule, ...]) -> str:
    return json.dumps([asdict(rule) for rule in rules], ensure_ascii=False)


def is_emoji(text: str) -> bool:
    return bool(text) and unicodedata.category(text[0]) == "So" and all(
        unicodedata.category(c) in _EMOJI_CATEGORIES for c in text
    )


def same_emoji(a: str, b: str) -> bool:
    """Keyboards differ on the invisible variation selector, as in 🛏 and 🛏️."""
    return a.replace(_VARIATION_SELECTOR, "") == b.replace(_VARIATION_SELECTOR, "")


def with_rule(rules: tuple[ReactionRule, ...], rule: ReactionRule) -> tuple[ReactionRule, ...]:
    """Add a rule, or replace the topic in place if the emoji already has one."""
    if any(same_emoji(r.emoji, rule.emoji) for r in rules):
        return tuple(rule if same_emoji(r.emoji, rule.emoji) else r for r in rules)
    return (*rules, rule)


def without_rule(rules: tuple[ReactionRule, ...], emoji: str) -> tuple[ReactionRule, ...]:
    return tuple(r for r in rules if not same_emoji(r.emoji, emoji))


def build_criteria(rules: tuple[ReactionRule, ...]) -> dict[str, str]:
    """The labels a decision model picks from: one per rule's emoji, plus NO_REACTION."""
    return {rule.emoji: rule.topic for rule in rules} | {NO_REACTION: "Anything else"}


def pick_rule(choice: Choice, rules: tuple[ReactionRule, ...]) -> ReactionRule | None:
    """The rule a decision selects. An unsure or unexpected one means no reaction."""
    if choice.confidence < MIN_CONFIDENCE:
        return None
    return next((rule for rule in rules if rule.emoji == choice.label), None)


@dataclass(frozen=True)
class ReactShow:
    """Show the rules and the usage. Also the answer to malformed commands."""


@dataclass(frozen=True)
class ReactAdd:
    emoji: str
    topic: str


@dataclass(frozen=True)
class ReactRemove:
    emoji: str


ReactCommand = ReactShow | ReactAdd | ReactRemove


def parse_react_command(text: str) -> ReactCommand | None:
    """Parse a /react command. Returns None if the text isn't one.

    Recognized forms:
      /react                       the rules and this usage
      /react add <emoji> <topic>   add a rule, or change the topic of that emoji
      /react remove <emoji>        remove a rule
    """
    words = text.split()
    if not words or words[0].lower() != "/react":
        return None
    match [word.lower() if i == 0 else word for i, word in enumerate(words[1:])]:
        case ["add", emoji, *topic] if topic:
            return ReactAdd(emoji, " ".join(topic))
        case ["remove", emoji]:
            return ReactRemove(emoji)
        case _:
            return ReactShow()
