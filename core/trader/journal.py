"""Trade journal — the crash-safe system of record (SQLite, WAL, synchronous=FULL).

Crash-safety rules (docs/ARCHITECTURE.md §5, no-UPS discipline):

1. Journal write *before* each exchange call, state-transition write *after* —
   state never lives only in memory.
2. WAL + ``synchronous=FULL``; a single writer owns all DB writes.
3. Client order IDs are a deterministic hash of the intent, so a crash-and-retry
   can never double-place an order.

Every state change also appends an immutable row to ``events`` — the audit trail
the reconciler (S2) and daily reports read from.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

-- Strategy intents: what the system decided to do, before any order exists.
CREATE TABLE IF NOT EXISTS intents (
    intent_id      TEXT PRIMARY KEY,          -- deterministic hash, see make_intent_id
    created_at     TEXT NOT NULL,             -- ISO-8601 UTC
    symbol         TEXT NOT NULL,
    side           TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
    order_type     TEXT NOT NULL,             -- LIMIT / MARKET / ...
    quantity       TEXT NOT NULL,             -- decimal string, exact
    price          TEXT,                      -- decimal string, NULL for MARKET
    source         TEXT NOT NULL,             -- strategy name / 'demo' / 'manual'
    status         TEXT NOT NULL DEFAULT 'NEW'
);

-- Orders: one row per intent actually sent to the exchange.
CREATE TABLE IF NOT EXISTS orders (
    client_order_id   TEXT PRIMARY KEY,       -- deterministic, derived from intent_id
    intent_id         TEXT NOT NULL REFERENCES intents(intent_id),
    exchange_order_id TEXT,                   -- set on exchange ack
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    order_type        TEXT NOT NULL,
    quantity          TEXT NOT NULL,
    price             TEXT,
    status            TEXT NOT NULL,          -- SUBMITTING → SUBMITTED → FILLED/CANCELED/...
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Fills: executions reported by the exchange.
CREATE TABLE IF NOT EXISTS fills (
    fill_id           TEXT PRIMARY KEY,       -- exchange trade id (unique per symbol+id)
    client_order_id   TEXT NOT NULL REFERENCES orders(client_order_id),
    quantity          TEXT NOT NULL,
    price             TEXT NOT NULL,
    fee               TEXT NOT NULL DEFAULT '0',
    fee_asset         TEXT,
    executed_at       TEXT NOT NULL
);

-- Immutable audit log of every state transition.
CREATE TABLE IF NOT EXISTS events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL,               -- INTENT_CREATED / ORDER_SUBMITTING / ...
    ref_id       TEXT NOT NULL,               -- intent_id or client_order_id
    payload      TEXT NOT NULL DEFAULT '{}'   -- JSON
);

CREATE INDEX IF NOT EXISTS idx_orders_intent ON orders(intent_id);
CREATE INDEX IF NOT EXISTS idx_events_ref ON events(ref_id);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def make_intent_id(
    symbol: str, side: str, order_type: str, quantity: str, price: str | None, nonce: str
) -> str:
    """Deterministic intent id. Same intent (incl. nonce) always hashes to the same id,
    so a crash-and-retry reuses the same client order id instead of double-placing."""
    raw = "|".join([symbol, side, order_type, quantity, price or "", nonce])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def client_order_id_for(intent_id: str) -> str:
    # Binance allows up to 36 chars of [a-zA-Z0-9-_.:/]; keep a recognizable prefix.
    return f"mnt-{intent_id[:28]}"


def new_nonce() -> str:
    return uuid.uuid4().hex


class Journal:
    """Single-writer trade journal. All writes go through this class; the owning
    asyncio task is the only writer (writes are cheap and serialized by design)."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"journal schema version {row['version']} != code {SCHEMA_VERSION}; "
                    "migration required"
                )

    def close(self) -> None:
        self._conn.close()

    # -- event helper -------------------------------------------------------

    def _event(self, kind: str, ref_id: str, payload: dict[str, Any] | None = None) -> None:
        self._conn.execute(
            "INSERT INTO events(ts, kind, ref_id, payload) VALUES (?,?,?,?)",
            (utcnow(), kind, ref_id, json.dumps(payload or {}, default=str)),
        )

    # -- intent lifecycle ----------------------------------------------------

    def record_intent(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: str,
        price: str | None,
        source: str,
        nonce: str,
    ) -> str:
        intent_id = make_intent_id(symbol, side, order_type, quantity, price, nonce)
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO intents"
                "(intent_id, created_at, symbol, side, order_type, quantity, price, source)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (intent_id, utcnow(), symbol, side, order_type, quantity, price, source),
            )
            self._event("INTENT_CREATED", intent_id, {"source": source})
        return intent_id

    # -- order lifecycle -----------------------------------------------------

    def record_order_submitting(self, intent_id: str) -> str:
        """MUST be called (and committed) before the network call to the exchange."""
        row = self._conn.execute(
            "SELECT * FROM intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown intent {intent_id}")
        coid = client_order_id_for(intent_id)
        now = utcnow()
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO orders"
                "(client_order_id, intent_id, symbol, side, order_type, quantity, price,"
                " status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,'SUBMITTING',?,?)",
                (
                    coid, intent_id, row["symbol"], row["side"], row["order_type"],
                    row["quantity"], row["price"], now, now,
                ),
            )
            self._conn.execute(
                "UPDATE intents SET status='SUBMITTED' WHERE intent_id=?", (intent_id,)
            )
            self._event("ORDER_SUBMITTING", coid, {"intent_id": intent_id})
        return coid

    def record_order_ack(self, client_order_id: str, exchange_order_id: str, status: str) -> None:
        """Exchange accepted the order — called right after the network call returns."""
        with self._conn:
            self._conn.execute(
                "UPDATE orders SET exchange_order_id=?, status=?, updated_at=?"
                " WHERE client_order_id=?",
                (exchange_order_id, status, utcnow(), client_order_id),
            )
            self._event(
                "ORDER_ACK", client_order_id,
                {"exchange_order_id": exchange_order_id, "status": status},
            )

    def record_order_status(
        self, client_order_id: str, status: str, payload: dict[str, Any] | None = None
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE orders SET status=?, updated_at=? WHERE client_order_id=?",
                (status, utcnow(), client_order_id),
            )
            self._event(f"ORDER_{status}", client_order_id, payload)

    def record_fill(
        self,
        *,
        client_order_id: str,
        fill_id: str,
        quantity: str,
        price: str,
        fee: str,
        fee_asset: str | None,
        executed_at: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO fills"
                "(fill_id, client_order_id, quantity, price, fee, fee_asset, executed_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (fill_id, client_order_id, quantity, price, fee, fee_asset, executed_at),
            )
            self._event("FILL", client_order_id, {"fill_id": fill_id, "qty": quantity})

    # -- reads ----------------------------------------------------------------

    def get_order(self, client_order_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
        ).fetchone()

    def events_for(self, ref_id: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM events WHERE ref_id=? ORDER BY event_id", (ref_id,)
        ).fetchall()

    def open_orders(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM orders WHERE status IN ('SUBMITTING','SUBMITTED','PARTIALLY_FILLED')"
        ).fetchall()
