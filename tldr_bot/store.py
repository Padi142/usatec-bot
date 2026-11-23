import json
import sqlite3
from dataclasses import asdict, dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT    NOT NULL,
    sender_id   TEXT    NOT NULL,
    sender_name TEXT    NOT NULL,
    timestamp   INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    UNIQUE (group_id, sender_id, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_messages_group_ts ON messages (group_id, timestamp);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Images people sent, so /sticker can find the one a reply quotes.
CREATE TABLE IF NOT EXISTS images (
    group_id      TEXT    NOT NULL,
    sender_id     TEXT    NOT NULL,
    timestamp     INTEGER NOT NULL,
    attachment_id TEXT    NOT NULL,
    PRIMARY KEY (group_id, sender_id, timestamp)
);
-- The shared sticker pack, in the order the pack shows them.
CREATE TABLE IF NOT EXISTS stickers (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    image BLOB    NOT NULL,
    emoji TEXT    NOT NULL
);
"""

# Pruning on every insert is wasteful; do it every N inserts instead.
PRUNE_EVERY = 100


@dataclass(frozen=True)
class ChatMessage:
    group_id: str
    sender_id: str
    sender_name: str
    timestamp: int  # milliseconds since epoch, as sent by Signal
    text: str


# Settings key for the current version of the shared sticker pack, as JSON.
STICKER_PACK_SETTING = "sticker_pack"


@dataclass(frozen=True)
class PackSticker:
    id: int
    image: bytes  # a 512x512 WebP
    emoji: str


@dataclass(frozen=True)
class PackRef:
    """One uploaded version of the sticker pack."""
    pack_id: str  # hex
    pack_key: str  # hex

    @property
    def url(self) -> str:
        return f"https://signal.art/addstickers/#pack_id={self.pack_id}&pack_key={self.pack_key}"


class MessageStore:
    def __init__(self, path: str, history_limit: int):
        self.history_limit = history_limit
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)
        self._inserts_since_prune = 0

    def close(self) -> None:
        self._db.close()

    def add(self, msg: ChatMessage) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO messages (group_id, sender_id, sender_name, timestamp, text)"
                " VALUES (?, ?, ?, ?, ?)",
                (msg.group_id, msg.sender_id, msg.sender_name, msg.timestamp, msg.text),
            )
        self._inserts_since_prune += 1
        if self._inserts_since_prune >= PRUNE_EVERY:
            self.prune()

    def edit(self, group_id: str, sender_id: str, target_timestamp: int, new_text: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE messages SET text = ? WHERE group_id = ? AND sender_id = ? AND timestamp = ?",
                (new_text, group_id, sender_id, target_timestamp),
            )

    def delete(self, group_id: str, sender_id: str, target_timestamp: int) -> None:
        with self._db:
            for table in ("messages", "images"):
                self._db.execute(
                    f"DELETE FROM {table} WHERE group_id = ? AND sender_id = ? AND timestamp = ?",
                    (group_id, sender_id, target_timestamp),
                )

    def add_image(self, group_id: str, sender_id: str, timestamp: int, attachment_id: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO images (group_id, sender_id, timestamp, attachment_id) VALUES (?, ?, ?, ?)",
                (group_id, sender_id, timestamp, attachment_id),
            )

    def image(self, group_id: str, sender_ids: tuple[str, ...], timestamp: int) -> str | None:
        """The attachment id of an image one of sender_ids sent at timestamp, if it was saved."""
        placeholders = ", ".join("?" * len(sender_ids))
        row = self._db.execute(
            f"SELECT attachment_id FROM images WHERE group_id = ? AND timestamp = ? AND sender_id IN ({placeholders})",
            (group_id, timestamp, *sender_ids),
        ).fetchone()
        return row[0] if row else None

    def stickers(self) -> list[PackSticker]:
        return [PackSticker(*row) for row in self._db.execute("SELECT id, image, emoji FROM stickers ORDER BY id")]

    def sticker_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM stickers").fetchone()[0]

    def sticker_pack(self) -> PackRef | None:
        """The current version of the shared sticker pack, None while it's empty."""
        raw = self.setting(STICKER_PACK_SETTING)
        return PackRef(**json.loads(raw)) if raw else None

    # Edits take the pack version that was uploaded with them, so the stored stickers
    # and the published pack change together or not at all.

    def add_sticker(self, image: bytes, emoji: str, pack: PackRef) -> None:
        with self._db:
            self._db.execute("INSERT INTO stickers (image, emoji) VALUES (?, ?)", (image, emoji))
            self._set_pack(pack)

    def remove_sticker(self, sticker_id: int, pack: PackRef | None) -> None:
        with self._db:
            self._db.execute("DELETE FROM stickers WHERE id = ?", (sticker_id,))
            self._set_pack(pack)

    def _set_pack(self, pack: PackRef | None) -> None:
        if pack is None:
            self._db.execute("DELETE FROM settings WHERE key = ?", (STICKER_PACK_SETTING,))
        else:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (STICKER_PACK_SETTING, json.dumps(asdict(pack))),
            )

    def last_before(
        self, group_id: str, before_timestamp: int, limit: int, after_timestamp: int | None = None
    ) -> list[ChatMessage]:
        """The `limit` most recent messages sent in (after_timestamp, before_timestamp), oldest first.

        `after_timestamp` is exclusive; None means "since the beginning".
        """
        sql = (
            "SELECT group_id, sender_id, sender_name, timestamp, text FROM messages"
            " WHERE group_id = ? AND timestamp < ?"
        )
        params: list[object] = [group_id, before_timestamp]
        if after_timestamp is not None:
            sql += " AND timestamp > ?"
            params.append(after_timestamp)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._db.execute(sql, params).fetchall()
        return [ChatMessage(*row) for row in reversed(rows)]

    def last_from(self, group_id: str, sender_id: str, before_timestamp: int) -> ChatMessage | None:
        """The sender's most recent message sent before `before_timestamp`, if any."""
        row = self._db.execute(
            "SELECT group_id, sender_id, sender_name, timestamp, text FROM messages"
            " WHERE group_id = ? AND sender_id = ? AND timestamp < ?"
            " ORDER BY timestamp DESC LIMIT 1",
            (group_id, sender_id, before_timestamp),
        ).fetchone()
        return ChatMessage(*row) if row else None

    def display_name(self, sender_id: str) -> str | None:
        row = self._db.execute(
            "SELECT sender_name FROM messages WHERE sender_id = ? ORDER BY timestamp DESC LIMIT 1",
            (sender_id,),
        ).fetchone()
        return row[0] if row else None

    def setting(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        """Store a setting that survives restarts. None removes it."""
        with self._db:
            if value is None:
                self._db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self._db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)"
                    " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def prune(self) -> None:
        """Keep only the newest `history_limit` messages, and as many images, per group."""
        with self._db:
            for table in ("messages", "images"):
                self._db.execute(
                    f"""
                    DELETE FROM {table} WHERE rowid IN (
                        SELECT rowid FROM (
                            SELECT rowid, ROW_NUMBER() OVER (
                                PARTITION BY group_id ORDER BY timestamp DESC
                            ) AS rn FROM {table}
                        ) WHERE rn > ?
                    )
                    """,
                    (self.history_limit,),
                )
        self._inserts_since_prune = 0
