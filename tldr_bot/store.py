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
    picture     TEXT,  -- what the vision model read in the message's pictures
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
-- Every uploaded version of the pack, since people keep using the versions they installed.
CREATE TABLE IF NOT EXISTS sticker_pack_versions (
    pack_id     TEXT PRIMARY KEY,
    sticker_ids TEXT NOT NULL  -- JSON list of stickers.id, in pack order
);
-- Messages that showed a sticker from the pack, so /sticker remove can find the one a reply quotes.
CREATE TABLE IF NOT EXISTS sticker_messages (
    group_id   TEXT    NOT NULL,
    timestamp  INTEGER NOT NULL,
    sticker_id INTEGER NOT NULL,  -- stickers.id
    PRIMARY KEY (group_id, timestamp)
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
    picture: str | None = None  # a description of its pictures, filled in after it's stored


# Settings key for the current version of the shared sticker pack, as JSON.
STICKER_PACK_SETTING = "sticker_pack"


@dataclass(frozen=True)
class PackSticker:
    id: int
    image: bytes  # a 512x512 PNG
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
        # Databases from before the picture column.
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(messages)")}
        if "picture" not in columns:
            with self._db:
                self._db.execute("ALTER TABLE messages ADD COLUMN picture TEXT")
        # Databases from before pack versions were kept know only the current one.
        if (pack := self.sticker_pack()) is not None:
            with self._db:
                self._add_pack_version(pack, ignore_existing=True)
        self._inserts_since_prune = 0

    def close(self) -> None:
        self._db.close()

    def add(self, msg: ChatMessage) -> bool:
        """Store a message. False if it was already stored, as when Signal delivers it again."""
        with self._db:
            inserted = self._db.execute(
                "INSERT OR IGNORE INTO messages (group_id, sender_id, sender_name, timestamp, text)"
                " VALUES (?, ?, ?, ?, ?)",
                (msg.group_id, msg.sender_id, msg.sender_name, msg.timestamp, msg.text),
            ).rowcount == 1
        self._inserts_since_prune += 1
        if self._inserts_since_prune >= PRUNE_EVERY:
            self.prune()
        return inserted

    def set_picture(self, group_id: str, sender_id: str, timestamp: int, description: str) -> None:
        """Attach a picture description to a stored message. Edits keep it, a delete drops it."""
        with self._db:
            self._db.execute(
                "UPDATE messages SET picture = ?"
                " WHERE group_id = ? AND sender_id = ? AND timestamp = ? AND picture IS NULL",
                (description, group_id, sender_id, timestamp),
            )

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

    def remove_stickers(self, sticker_ids: list[int], pack: PackRef | None) -> None:
        with self._db:
            self._db.executemany("DELETE FROM stickers WHERE id = ?", [(i,) for i in sticker_ids])
            self._set_pack(pack)

    def set_sticker_emoji(self, sticker_id: int, emoji: str, pack: PackRef) -> None:
        with self._db:
            self._db.execute("UPDATE stickers SET emoji = ? WHERE id = ?", (emoji, sticker_id))
            self._set_pack(pack)

    def set_sticker_image(self, sticker_id: int, image: bytes) -> None:
        """Replace a sticker's picture with the same picture in another format. The pack stays as it is."""
        with self._db:
            self._db.execute("UPDATE stickers SET image = ? WHERE id = ?", (image, sticker_id))

    def _set_pack(self, pack: PackRef | None) -> None:
        if pack is None:
            self._db.execute("DELETE FROM settings WHERE key = ?", (STICKER_PACK_SETTING,))
        else:
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (STICKER_PACK_SETTING, json.dumps(asdict(pack))),
            )
            self._add_pack_version(pack)

    def _add_pack_version(self, pack: PackRef, ignore_existing: bool = False) -> None:
        """Record which stored sticker each sticker of a pack version is: the stored ones, in order."""
        ids = [row[0] for row in self._db.execute("SELECT id FROM stickers ORDER BY id")]
        self._db.execute(
            f"INSERT {'OR IGNORE' if ignore_existing else 'OR REPLACE'} INTO sticker_pack_versions"
            " (pack_id, sticker_ids) VALUES (?, ?)",
            (pack.pack_id, json.dumps(ids)),
        )

    def add_sticker_message(self, group_id: str, timestamp: int, pack_id: str, sticker_index: int) -> None:
        """Remember a message that showed a sticker. Only stickers from a version of the pack are kept."""
        row = self._db.execute(
            "SELECT sticker_ids FROM sticker_pack_versions WHERE pack_id = ?", (pack_id,)
        ).fetchone()
        ids = json.loads(row[0]) if row else []
        if 0 <= sticker_index < len(ids):
            with self._db:
                self._db.execute(
                    "INSERT OR IGNORE INTO sticker_messages (group_id, timestamp, sticker_id) VALUES (?, ?, ?)",
                    (group_id, timestamp, ids[sticker_index]),
                )

    def sticker_message(self, group_id: str, timestamp: int) -> int | None:
        """The stored sticker a message showed, if it's still in the pack."""
        row = self._db.execute(
            "SELECT m.sticker_id FROM sticker_messages m JOIN stickers s ON s.id = m.sticker_id"
            " WHERE m.group_id = ? AND m.timestamp = ?",
            (group_id, timestamp),
        ).fetchone()
        return row[0] if row else None

    def last_before(
        self, group_id: str, before_timestamp: int, limit: int, after_timestamp: int | None = None
    ) -> list[ChatMessage]:
        """The `limit` most recent messages sent in (after_timestamp, before_timestamp), oldest first.

        `after_timestamp` is exclusive; None means "since the beginning".
        """
        sql = (
            "SELECT group_id, sender_id, sender_name, timestamp, text, picture FROM messages"
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
            "SELECT group_id, sender_id, sender_name, timestamp, text, picture FROM messages"
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
        """Keep only the newest `history_limit` messages, and as many images and stickers, per group."""
        with self._db:
            for table in ("messages", "images", "sticker_messages"):
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
