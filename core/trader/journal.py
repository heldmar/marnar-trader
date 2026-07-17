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
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 5

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

-- v2 (Sprint 2) --------------------------------------------------------------

-- Open spot positions, maintained from fills (BUY adds, SELL reduces).
CREATE TABLE IF NOT EXISTS positions (
    symbol      TEXT PRIMARY KEY,
    quantity    TEXT NOT NULL,                 -- decimal string, base asset
    cost        TEXT NOT NULL,                 -- quote spent on the open quantity
    updated_at  TEXT NOT NULL
);

-- Realized P&L ledger: one row per position-reducing fill (SELL on spot).
CREATE TABLE IF NOT EXISTS pnl_ledger (
    entry_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,                -- decimal string, quote asset
    fee          TEXT NOT NULL DEFAULT '0'
);

-- Durable key/value state: halt state, equity peak, daily anchor.
-- Circuit-breaker halts MUST survive a crash/restart (D-07: drawdown halt
-- requires manual reactivation), so they live here, never only in memory.
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,                  -- JSON
    updated_at TEXT NOT NULL
);

-- v3 (Sprint 5) --------------------------------------------------------------

-- News headlines (D-23: ingested from S5, shown as trade-timeline context at
-- S6). Context only — no strategy reads this table; truthful attribution.
CREATE TABLE IF NOT EXISTS news_items (
    url        TEXT PRIMARY KEY,               -- dedupe key
    seen_at    TEXT NOT NULL,                  -- when WE stored it (ISO-8601 UTC)
    published  TEXT,                           -- source-claimed publication time
    source     TEXT,                           -- domain
    title      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_seen ON news_items(seen_at);

-- v4 (Sprint 6) --------------------------------------------------------------

-- Periodic mark-to-market equity snapshots (UI equity curve; engine writes
-- one at most every snapshot interval — a few KB per week).
CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts     TEXT PRIMARY KEY,                   -- ISO-8601 UTC
    equity TEXT NOT NULL                       -- decimal string, USDT
);

-- v5 (Sprint 7) --------------------------------------------------------------

-- Generated investor reports (S7): daily and weekly, rendered once and
-- archived here for the UI; the compact Telegram copy is derived from the
-- same summary at send time. Idempotency key = (kind, period).
CREATE TABLE IF NOT EXISTS reports (
    kind         TEXT NOT NULL CHECK (kind IN ('daily','weekly')),
    period       TEXT NOT NULL,                -- '2026-07-16' or '2026-W29'
    generated_at TEXT NOT NULL,                -- ISO-8601 UTC
    summary      TEXT NOT NULL,                -- JSON (P&L, equity, counts...)
    body_md      TEXT NOT NULL,                -- full markdown report
    PRIMARY KEY (kind, period)
);
"""

# Tables added by each version bump; applying the base _SCHEMA (all CREATE IF
# NOT EXISTS) migrates any older DB forward, so migrations stay additive-only.
_ADDITIVE_VERSIONS = {1, 2, 3, 4, 5}


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
        # check_same_thread=False: the FastAPI event loop (all endpoints) and
        # the engine share one loop thread in production; sqlite3 is built
        # serialized, and our single-writer discipline holds regardless.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # QA1-09/QA2-03: engine and report cycles now run in worker threads
        # (asyncio.to_thread) while API endpoints stay on the event loop. The
        # lock keeps each `with journal._lock, journal._conn:` transaction
        # atomic across threads — the single-writer discipline, enforced.
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif row["version"] < SCHEMA_VERSION and row["version"] in _ADDITIVE_VERSIONS:
                # Additive-only migration: the executescript above already created
                # the missing tables; just stamp the new version.
                self._conn.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
                self._event("SCHEMA_MIGRATED", "journal",
                            {"from": row["version"], "to": SCHEMA_VERSION})
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

    def record_event(self, kind: str, ref_id: str, payload: dict[str, Any] | None = None) -> None:
        """Standalone committed audit event (risk rejections, halt execution, ...)."""
        with self._lock, self._conn:
            self._event(kind, ref_id, payload)

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
        with self._lock, self._conn:
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
        with self._lock, self._conn:
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
        with self._lock, self._conn:
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
        with self._lock, self._conn:
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
        """Record a fill and, in the same transaction, update the position and
        (on SELL) the realized-P&L ledger. Idempotent: a fill_id seen before is
        ignored and does NOT re-apply to the position."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO fills"
                "(fill_id, client_order_id, quantity, price, fee, fee_asset, executed_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (fill_id, client_order_id, quantity, price, fee, fee_asset, executed_at),
            )
            if cur.rowcount == 0:
                return  # duplicate fill (crash-retry / reconciler resync)
            self._event("FILL", client_order_id, {"fill_id": fill_id, "qty": quantity})
            order = self._conn.execute(
                "SELECT symbol, side FROM orders WHERE client_order_id=?", (client_order_id,)
            ).fetchone()
            if order is not None:
                self._apply_fill_to_position(
                    symbol=order["symbol"], side=order["side"],
                    quantity=Decimal(quantity), price=Decimal(price), fee=Decimal(fee),
                )

    def _apply_fill_to_position(
        self, *, symbol: str, side: str, quantity: Decimal, price: Decimal, fee: Decimal
    ) -> None:
        """Position bookkeeping (spot): BUY adds quantity at cost; SELL reduces it
        and realizes P&L against the average entry cost. Runs inside record_fill's
        transaction — never call it standalone."""
        now = utcnow()
        row = self._conn.execute(
            "SELECT quantity, cost FROM positions WHERE symbol=?", (symbol,)
        ).fetchone()
        if side == "BUY":
            # QA1-16 (D-34): the entry fee is part of what the position cost —
            # realized P&L on the eventual SELL is measured against it, so a
            # round trip that only covers one fee is truthfully a loss. The
            # backtester uses the same convention (backtest.py fill()).
            if row is None:
                qty, cost = quantity, quantity * price + fee
            else:
                qty = Decimal(row["quantity"]) + quantity
                cost = Decimal(row["cost"]) + quantity * price + fee
            self._conn.execute(
                "INSERT INTO positions(symbol, quantity, cost, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(symbol) DO UPDATE SET quantity=?, cost=?, updated_at=?",
                (symbol, str(qty), str(cost), now, str(qty), str(cost), now),
            )
            return
        # SELL
        if row is None:
            # Selling with no journaled position (e.g. pre-existing balance adopted
            # mid-flight). Nothing to realize against — flag it for the reconciler.
            self._event("FILL_WITHOUT_POSITION", symbol, {"side": side, "qty": str(quantity)})
            return
        held_qty, held_cost = Decimal(row["quantity"]), Decimal(row["cost"])
        sell_qty = min(quantity, held_qty)
        avg_cost = held_cost / held_qty
        realized = (price - avg_cost) * sell_qty - fee
        self._conn.execute(
            "INSERT INTO pnl_ledger(ts, symbol, realized_pnl, fee) VALUES (?,?,?,?)",
            (now, symbol, str(realized), str(fee)),
        )
        remaining = held_qty - sell_qty
        if remaining > 0:
            self._conn.execute(
                "UPDATE positions SET quantity=?, cost=?, updated_at=? WHERE symbol=?",
                (str(remaining), str(avg_cost * remaining), now, symbol),
            )
        else:
            self._conn.execute("DELETE FROM positions WHERE symbol=?", (symbol,))

    # -- durable app state (halt state, equity peak, day anchor) ---------------

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return default if row is None else json.loads(row["value"])

    def set_state(self, key: str, value: Any) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO app_state(key, value, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " updated_at=excluded.updated_at",
                (key, json.dumps(value, default=str), utcnow()),
            )

    # -- news (D-23a: context only, no strategy reads this) --------------------

    def record_news_item(
        self, *, url: str, title: str, source: str | None, published: str | None, seen_at: str
    ) -> bool:
        """Store a headline; returns False if the URL was already known."""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO news_items(url, seen_at, published, source, title)"
                " VALUES (?,?,?,?,?)",
                (url, seen_at, published, source, title),
            )
            return cur.rowcount > 0

    def news_since(self, ts_iso: str) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM news_items WHERE seen_at >= ? ORDER BY seen_at", (ts_iso,)
        ).fetchall()

    # -- equity snapshots (S6: UI equity curve) --------------------------------

    def record_equity_snapshot(self, equity: str, ts: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO equity_snapshots(ts, equity) VALUES (?,?)",
                (ts or utcnow(), equity),
            )

    def equity_at(self, ts_iso: str) -> sqlite3.Row | None:
        """Latest snapshot at or before *ts_iso* (S7 report period boundaries)."""
        return self._conn.execute(
            "SELECT ts, equity FROM equity_snapshots WHERE ts <= ?"
            " ORDER BY ts DESC LIMIT 1",
            (ts_iso,),
        ).fetchone()

    def equity_series(self, since_iso: str = "") -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT ts, equity FROM equity_snapshots WHERE ts >= ? ORDER BY ts",
            (since_iso,),
        ).fetchall()

    # -- reports (S7: daily/weekly investor reports, UI archive) ---------------

    def save_report(
        self, *, kind: str, period: str, summary: dict[str, Any], body_md: str
    ) -> None:
        """Store (or regenerate) a report. (kind, period) is the idempotency key."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO reports(kind, period, generated_at, summary, body_md)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(kind, period) DO UPDATE SET"
                " generated_at=excluded.generated_at, summary=excluded.summary,"
                " body_md=excluded.body_md",
                (kind, period, utcnow(), json.dumps(summary, default=str), body_md),
            )

    @staticmethod
    def _period_start(kind: str, period: str) -> str:
        """Calendar start date of a report period, for sorting. Daily periods
        are their own date; weekly '2026-W29' starts on that ISO week's Monday.
        (QA2-04: raw string order puts every 'YYYY-Wnn' above every date.)"""
        if kind == "weekly":
            year, week = int(period[:4]), int(period.split("-W")[1])
            return date.fromisocalendar(year, week, 1).isoformat()
        return period

    def reports_list(self, limit: int = 100) -> list[sqlite3.Row]:
        """Newest-first report index (no bodies — the archive list view).
        Sorted by true period start date; a week sorts just above its Monday."""
        rows = self._conn.execute(
            "SELECT kind, period, generated_at, summary FROM reports"
        ).fetchall()
        rows.sort(
            key=lambda r: (self._period_start(r["kind"], r["period"]), r["kind"]),
            reverse=True,
        )
        return rows[:limit]

    def get_report(self, kind: str, period: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT kind, period, generated_at, summary, body_md FROM reports"
            " WHERE kind=? AND period=?",
            (kind, period),
        ).fetchone()

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
            "SELECT * FROM orders WHERE status IN "
            "('SUBMITTING','SUBMITTED','NEW','PARTIALLY_FILLED')"
        ).fetchall()

    def positions(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM positions ORDER BY symbol").fetchall()

    def position_for(self, symbol: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM positions WHERE symbol=?", (symbol,)
        ).fetchone()

    def fills_with_side(self) -> list[sqlite3.Row]:
        """All fills joined to their order's side (parity report input)."""
        return self._conn.execute(
            "SELECT o.side AS side, o.symbol AS symbol, f.quantity AS quantity,"
            " f.price AS price, f.fee AS fee, f.executed_at AS executed_at"
            " FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id"
            " ORDER BY f.executed_at"
        ).fetchall()

    def fills_between(self, lo_iso: str, hi_iso: str) -> list[sqlite3.Row]:
        """Side-joined fills within [lo, hi) — period-bounded (QA2-06)."""
        return self._conn.execute(
            "SELECT o.side AS side, o.symbol AS symbol, f.quantity AS quantity,"
            " f.price AS price, f.fee AS fee, f.executed_at AS executed_at"
            " FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id"
            " WHERE f.executed_at >= ? AND f.executed_at < ?"
            " ORDER BY f.executed_at",
            (lo_iso, hi_iso),
        ).fetchall()

    def events_of_kind_between(
        self, kinds: list[str], lo_iso: str, hi_iso: str
    ) -> list[sqlite3.Row]:
        """Events of the given kinds within [lo, hi) — no global row cap, so a
        regenerated old period can never silently lose rows (QA2-06)."""
        marks = ",".join("?" for _ in kinds)
        return self._conn.execute(
            f"SELECT ts, kind, ref_id, payload FROM events WHERE kind IN ({marks})"
            " AND ts >= ? AND ts < ? ORDER BY event_id",
            (*kinds, lo_iso, hi_iso),
        ).fetchall()

    def news_count_between(self, lo_iso: str, hi_iso: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM news_items WHERE seen_at >= ? AND seen_at < ?",
            (lo_iso, hi_iso),
        ).fetchone()
        return int(row["n"])

    def news_between(self, lo_iso: str, hi_iso: str) -> list[sqlite3.Row]:
        """Headlines within [lo, hi) — one batched query for a whole timeline
        page instead of one window query per trade (QA1-04/QA1-20)."""
        return self._conn.execute(
            "SELECT * FROM news_items WHERE seen_at >= ? AND seen_at < ?"
            " ORDER BY seen_at",
            (lo_iso, hi_iso),
        ).fetchall()

    def trade_history(
        self,
        symbol: str | None = None,
        limit: int = 200,
        *,
        since_iso: str | None = None,
        before_iso: str | None = None,
    ) -> list[sqlite3.Row]:
        """Executed orders newest-first with aggregated fill price/qty/fee and the
        intent's source — the UI trade timeline's raw material. The aggregated
        money columns are REAL (display only; exact math stays in Decimal paths).
        ``since_iso``/``before_iso`` bound by last execution time (QA2-06)."""
        where, params = "WHERE f.fill_id IS NOT NULL", []
        if symbol:
            where += " AND o.symbol = ?"
            params.append(symbol)
        having, hparams = "", []
        if since_iso is not None:
            having += " HAVING MAX(f.executed_at) >= ?"
            hparams.append(since_iso)
        if before_iso is not None:
            having += (" AND" if having else " HAVING") + " MAX(f.executed_at) < ?"
            hparams.append(before_iso)
        return self._conn.execute(
            "SELECT o.client_order_id, o.symbol, o.side, o.status, o.created_at,"
            " i.source AS source,"
            " SUM(CAST(f.quantity AS REAL)) AS qty,"
            " SUM(CAST(f.quantity AS REAL) * CAST(f.price AS REAL))"
            "   / SUM(CAST(f.quantity AS REAL)) AS avg_price,"
            " SUM(CAST(f.fee AS REAL)) AS fee,"
            " MAX(f.executed_at) AS executed_at"
            " FROM orders o"
            " JOIN intents i ON i.intent_id = o.intent_id"
            " LEFT JOIN fills f ON f.client_order_id = o.client_order_id"
            f" {where}"
            " GROUP BY o.client_order_id"
            f"{having}"
            " ORDER BY executed_at DESC LIMIT ?",
            (*params, *hparams, limit),
        ).fetchall()

    def pnl_entries_since(self, ts_iso: str = "") -> list[sqlite3.Row]:
        """Realized P&L ledger rows (one per position-reducing fill), oldest first."""
        return self._conn.execute(
            "SELECT ts, symbol, realized_pnl, fee FROM pnl_ledger WHERE ts >= ? ORDER BY ts",
            (ts_iso,),
        ).fetchall()

    def fees_total(self) -> Decimal:
        # QA1-17: money aggregation in Decimal, not IEEE floats — row counts
        # are small (one per fill) and the schema stores exact decimal strings.
        rows = self._conn.execute(
            "SELECT fee FROM fills WHERE fee_asset = 'USDT' OR fee_asset IS NULL"
        ).fetchall()
        return sum((Decimal(r["fee"]) for r in rows), Decimal("0"))

    def events_of_kind(self, kinds: list[str], limit: int = 100) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in kinds)
        return self._conn.execute(
            f"SELECT ts, kind, ref_id, payload FROM events WHERE kind IN ({marks})"
            " ORDER BY event_id DESC LIMIT ?",
            (*kinds, limit),
        ).fetchall()

    def news_page(self, limit: int = 50, before_iso: str | None = None) -> list[sqlite3.Row]:
        if before_iso:
            return self._conn.execute(
                "SELECT * FROM news_items WHERE seen_at < ? ORDER BY seen_at DESC LIMIT ?",
                (before_iso, limit),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM news_items ORDER BY seen_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def news_around(self, ts_iso: str, hours: float = 24.0) -> list[sqlite3.Row]:
        """Headlines seen within ±hours of *ts_iso* (timeline context, D-23)."""
        ts = datetime.fromisoformat(ts_iso)
        lo = (ts - timedelta(hours=hours)).isoformat(timespec="milliseconds")
        hi = (ts + timedelta(hours=hours)).isoformat(timespec="milliseconds")
        return self._conn.execute(
            "SELECT * FROM news_items WHERE seen_at BETWEEN ? AND ?"
            " ORDER BY seen_at DESC LIMIT 20",
            (lo, hi),
        ).fetchall()

    def realized_pnl_since(self, ts_iso: str) -> Decimal:
        rows = self._conn.execute(
            "SELECT realized_pnl FROM pnl_ledger WHERE ts >= ?", (ts_iso,)
        ).fetchall()
        return sum((Decimal(r["realized_pnl"]) for r in rows), Decimal("0"))

    def orders_created_since(self, ts_iso: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE created_at >= ?", (ts_iso,)
        ).fetchone()
        return int(row["n"])
