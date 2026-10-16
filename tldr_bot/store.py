import sqlite3
from dataclasses import dataclass

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
            self._db.execute(
                "DELETE FROM messages WHERE group_id = ? AND sender_id = ? AND timestamp = ?",
                (group_id, sender_id, target_timestamp),
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

    def prune(self) -> None:
        """Keep only the newest `history_limit` messages per group."""
        with self._db:
            self._db.execute(
                """
                DELETE FROM messages WHERE id IN (
                    SELECT id FROM (
                        SELECT id, ROW_NUMBER() OVER (
                            PARTITION BY group_id ORDER BY timestamp DESC
                        ) AS rn FROM messages
                    ) WHERE rn > ?
                )
                """,
                (self.history_limit,),
            )
        self._inserts_since_prune = 0
