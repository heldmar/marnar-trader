"""Web UI API (Sprint 6) — read endpoints for the dashboard/timeline and the
config editor. Mounted by main.py; everything reads the journal (system of
record) plus live prices from the gateway.

Design constraints honored:
- D-23 truthful attribution: trade reasons come from journaled TRADE_REASON
  events (the rule + numbers that actually fired); news is returned as
  *context*, in a separate field, never as a cause.
- Config writes may only tighten risk (RiskLimits floors enforce this at
  validation) and never touch secrets. A material strategy-parameter change
  resets the D-27 paper clock, so the endpoint demands an explicit
  acknowledgment flag and journals the change.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError

from trader.config import RISK_FLOORS, AppConfig, PaperConfig, RiskLimits, save_config
from trader.reports import DELIVERY_KEY

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# CSRF defense for state-changing endpoints (QA1-01): cross-site HTML forms
# cannot set custom headers, and cross-origin fetch with one triggers a CORS
# preflight this app never grants. The UI sends it on every write request.
CSRF_HEADER = "x-marnar-ui"


def require_ui_header(request: Request) -> None:
    if CSRF_HEADER not in request.headers:
        raise HTTPException(
            403,
            f"missing {CSRF_HEADER} header (state-changing requests must be "
            "deliberate; from curl add: -H 'x-marnar-ui: 1')",
        )

# Strategy-parameter fields whose change invalidates the running paper
# experiment (D-27: the clock restarts on a material strategy/risk change).
# QA1-15: event_blackout changes what the strategy may do — it belongs here.
CLOCK_RESETTING_FIELDS = {
    "symbols", "interval", "entry_n", "exit_n", "stop_loss_pct", "spend_pct",
    "event_blackout",
}


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def _downsample(points: list[dict], max_points: int = 500) -> list[dict]:
    if len(points) <= max_points:
        return points
    stride = -(-len(points) // max_points)  # ceil division
    sampled = points[::stride]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])  # the newest point always survives
    return sampled


def _prices(request: Request, symbols: list[str]) -> dict[str, Decimal]:
    gw = request.app.state.gateway
    prices: dict[str, Decimal] = {}
    for s in symbols:
        try:
            prices[s] = gw.ticker_price(s)
        except Exception as exc:  # price feed hiccup: report partial data, don't 500
            log.warning("ticker price failed for %s: %s", s, exc)
    return prices


@router.get("/overview")
async def overview(request: Request) -> dict:
    """Top onion layer (UI-13): how is it going — visual-first payload."""
    state = request.app.state
    journal, config = state.journal, state.config
    # D-27/D-34: a clock reset re-baselines the run, so every figure below is
    # measured from that boundary. Without this the previous run's history —
    # including whatever incident prompted the reset — is reported as this
    # run's track record, which is exactly backwards.
    since = journal.paper_started_iso()
    baseline = journal.paper_baseline_equity()
    initial = baseline if baseline is not None else Decimal(str(config.paper.initial_usdt))

    positions = journal.positions()
    equity: Decimal | None = None
    if state.gateway is not None:
        try:
            equity = Decimal(str(state.gateway.equity_usdt()))
        except Exception as exc:
            log.warning("equity unavailable: %s", exc)

    risk_status = state.risk.status()
    anchor = risk_status.get("day_anchor") or {}
    today_pnl = None
    if equity is not None and anchor.get("equity"):
        today_pnl = float(equity - Decimal(anchor["equity"]))

    pnl_rows = journal.pnl_entries_since(since)
    wins = [r for r in pnl_rows if Decimal(r["realized_pnl"]) > 0]

    started = journal.get_state("engine:paper_started_at")
    return {
        "mode": config.mode,
        "risk_state": risk_status["state"],
        "halt_reason": risk_status.get("reason"),
        "trading_enabled": state.trading_enabled,
        "paper_started_at": started,
        "initial_usdt": float(initial),
        "equity_usdt": _f(equity),
        "today_pnl_usdt": today_pnl,
        "total_pnl_usdt": _f(equity - initial) if equity is not None else None,
        "open_positions": len(positions),
        # QA1-20: the snapshot series grows one point / 15 min forever — cap the
        # payload by stride-sampling down to ~500 points (always keep the last).
        "equity_series": _downsample(
            [{"ts": r["ts"], "equity": float(r["equity"])} for r in journal.equity_series(since)]
        ),
        "trade_results": [  # win/loss bars (UI-15): one bar per closed trade
            {"ts": r["ts"], "symbol": r["symbol"], "pnl": float(r["realized_pnl"])}
            for r in pnl_rows
        ],
        "closed_trades": len(pnl_rows),
        "winning_trades": len(wins),
        "fees_usdt": float(journal.fees_total(since)),
    }


@router.get("/positions")
async def positions(request: Request) -> list[dict]:
    """Middle layer: each open position with live price, unrealized P&L and
    where the price sits between protective stop and entry."""
    journal = request.app.state.journal
    rows = journal.positions()
    prices = _prices(request, [p["symbol"] for p in rows])
    out = []
    for p in rows:
        symbol = p["symbol"]
        qty, cost = Decimal(p["quantity"]), Decimal(p["cost"])
        entry = cost / qty if qty else Decimal(0)
        price = prices.get(symbol)
        protect = journal.get_state(f"engine:protect:{symbol}") or {}
        out.append({
            "symbol": symbol,
            "quantity": float(qty),
            "entry_price": float(entry),
            "current_price": _f(price),
            "cost_usdt": float(cost),
            "value_usdt": _f(qty * price) if price is not None else None,
            "unrealized_pnl_usdt": _f(qty * price - cost) if price is not None else None,
            "stop_price": _f(Decimal(protect["stop"])) if protect.get("stop") else None,
            "take_profit_price": _f(Decimal(protect["tp"])) if protect.get("tp") else None,
            "opened_at": p["updated_at"],
        })
    return out


@router.get("/timeline")
async def timeline(
    request: Request,
    symbol: str | None = None,
    limit: int = Query(100, ge=1, le=500),  # QA1-04: no unbounded/negative dumps
) -> list[dict]:
    """Deep layer: every executed trade with its journaled reason (D-23:
    the rule + numbers that fired) and nearby headlines as separate context."""
    journal = request.app.state.journal
    trades = journal.trade_history(symbol=symbol, limit=limit)
    # QA1-04/QA1-20: one batched news query for the whole page, bucketed per
    # trade in Python — not one ±24h window query per trade (the old N+1).
    def _parseable(ts: str) -> bool:
        # QA1-13: tolerate a malformed row (e.g. a legacy epoch-ms stamp) —
        # it just gets no news context instead of 500ing the whole timeline.
        try:
            datetime.fromisoformat(ts)
            return True
        except ValueError:
            return False

    stamps = sorted(
        ts for t in trades if (ts := t["executed_at"] or t["created_at"]) and _parseable(ts)
    )
    news_by_ts: dict[str, list] = {}
    if stamps:
        window = timedelta(hours=24.0)
        lo = (datetime.fromisoformat(stamps[0]) - window).isoformat(timespec="milliseconds")
        hi = (datetime.fromisoformat(stamps[-1]) + window).isoformat(timespec="milliseconds")
        all_news = journal.news_between(lo, hi)
        for ts in stamps:
            t0 = (datetime.fromisoformat(ts) - window).isoformat(timespec="milliseconds")
            t1 = (datetime.fromisoformat(ts) + window).isoformat(timespec="milliseconds")
            news_by_ts[ts] = [n for n in all_news if t0 <= n["seen_at"] <= t1]
    items: list[dict] = []
    for t in trades:
        reason = None
        for ev in journal.events_for(t["client_order_id"]):
            if ev["kind"] == "TRADE_REASON":
                reason = json.loads(ev["payload"])
        ts = t["executed_at"] or t["created_at"]
        items.append({
            "type": "trade",
            "ts": ts,
            "symbol": t["symbol"],
            "side": t["side"],
            "quantity": t["qty"],
            "avg_price": t["avg_price"],
            "fee_usdt": t["fee"],
            "source": t["source"],
            "reason": reason,  # rule + numbers, or null for pre-S6 trades
            "news_context": [  # context only — never presented as the cause
                {"seen_at": n["seen_at"], "source": n["source"],
                 "title": n["title"], "url": n["url"]}
                for n in news_by_ts.get(ts, [])
            ] if ts else [],
        })
    system_kinds = ["PAPER_CLOCK_STARTED", "PAPER_CLOCK_RESET", "HALT_EXECUTED", "RESUMED",
                    "CONFIG_CHANGED", "ENTRY_SKIPPED_MIN_NOTIONAL", "RISK_REJECTED"]
    for ev in journal.events_of_kind(system_kinds, limit=limit):
        items.append({
            "type": "system",
            "ts": ev["ts"],
            "kind": ev["kind"],
            "detail": json.loads(ev["payload"]),
        })
    items.sort(key=lambda x: x["ts"] or "", reverse=True)
    return items[:limit]


@router.get("/news")
async def news(
    request: Request,
    limit: int = Query(50, ge=1, le=200),  # QA1-04
    before: str | None = None,
) -> list[dict]:
    return [dict(n) for n in request.app.state.journal.news_page(limit=limit, before_iso=before)]


@router.get("/reports")
async def reports_index(
    request: Request, limit: int = Query(100, ge=1, le=500)  # QA2-05
) -> list[dict]:
    """S7 report archive index, newest first (no bodies — see the detail route)."""
    return [
        {
            "kind": r["kind"],
            "period": r["period"],
            "generated_at": r["generated_at"],
            "summary": json.loads(r["summary"]),
        }
        for r in request.app.state.journal.reports_list(limit=limit)
    ]


class ReportSettings(BaseModel):
    telegram_enabled: bool


@router.get("/reports/settings")
async def report_settings(request: Request) -> dict:
    """Delivery switch for the Telegram copy of daily/weekly reports. Reports
    are always archived regardless; this only controls the push."""
    journal = request.app.state.journal
    return {"telegram_enabled": bool(journal.get_state(DELIVERY_KEY, True))}


@router.put("/reports/settings")
async def put_report_settings(request: Request, settings: ReportSettings) -> dict:
    require_ui_header(request)
    journal = request.app.state.journal
    journal.set_state(DELIVERY_KEY, settings.telegram_enabled)
    journal.record_event(
        "REPORT_DELIVERY_CHANGED", "reports",
        {"telegram_enabled": settings.telegram_enabled, "via": "ui"},
    )
    return {"telegram_enabled": settings.telegram_enabled}


@router.get("/reports/{kind}/{period}")
async def report_detail(request: Request, kind: str, period: str) -> dict:
    row = request.app.state.journal.get_report(kind, period)
    if row is None:
        raise HTTPException(404, f"no {kind} report for {period}")
    return {
        "kind": row["kind"],
        "period": row["period"],
        "generated_at": row["generated_at"],
        "summary": json.loads(row["summary"]),
        "body_md": row["body_md"],
    }


@router.get("/config")
async def get_config(request: Request) -> dict:
    """Effective config with the non-negotiable risk floors marked (UI shows
    floors as read-only bounds — config may only tighten, D-06/07/08)."""
    config: AppConfig = request.app.state.config
    return {
        "mode": config.mode,
        "paper": config.paper.model_dump(mode="json"),
        "risk": config.risk.model_dump(mode="json"),
        "risk_floors": RISK_FLOORS,
        "clock_resetting_fields": sorted(CLOCK_RESETTING_FIELDS),
    }


class ConfigUpdate(BaseModel):
    paper: dict[str, Any] = {}
    risk: dict[str, Any] = {}
    # Must be true when a CLOCK_RESETTING_FIELD changes — the UI collects the
    # explicit acknowledgment that the D-27 paper clock restarts.
    acknowledge_clock_reset: bool = False


@router.put("/config")
async def put_config(request: Request, update: ConfigUpdate) -> dict:
    require_ui_header(request)
    state = request.app.state
    config: AppConfig = state.config
    if "initial_usdt" in update.paper:
        # QA1-15: changing it after first boot silently does nothing to the
        # account balance and skews every P&L-vs-start figure. Reject outright;
        # a different capital base means a fresh paper run (delete the volume).
        raise HTTPException(
            422,
            "initial_usdt cannot be changed on a running paper account — "
            "reset the paper run to start with different capital",
        )
    try:
        new_paper = PaperConfig(**{**config.paper.model_dump(), **update.paper})
        new_risk = RiskLimits(**{**config.risk.model_dump(), **update.risk})
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc

    changed_strategy = {
        f for f in CLOCK_RESETTING_FIELDS
        if getattr(new_paper, f) != getattr(config.paper, f)
    }
    if changed_strategy and not update.acknowledge_clock_reset:
        raise HTTPException(
            409,
            "changing "
            + ", ".join(sorted(changed_strategy))
            + " resets the paper-trading clock (D-27); repeat with"
            " acknowledge_clock_reset=true to confirm",
        )

    new_config = config.model_copy(update={"paper": new_paper, "risk": new_risk})
    from trader.main import CONFIG_PATH  # circular-safe: value import at call time

    save_config(new_config, CONFIG_PATH)
    state.config = new_config
    # Hot-apply: a tightened risk limit binds now, not at next restart (QA1-10).
    state.risk.set_limits(new_risk)
    state.journal.record_event(
        "CONFIG_CHANGED", "config",
        {"paper": update.paper, "risk": update.risk,
         "clock_reset_fields": sorted(changed_strategy)},
    )
    if changed_strategy:
        # D-27: material strategy change — restart the paper clock deliberately.
        state.journal.set_state("engine:paper_started_at", None)
        state.journal.record_event("PAPER_CLOCK_RESET", "config",
                                   {"fields": sorted(changed_strategy)})
    return {
        "saved": True,
        "restart_required": True,  # engine reads strategy params at boot
        "clock_reset": bool(changed_strategy),
    }


@router.get("/system")
async def system(request: Request) -> dict:
    """Health strip: freshness of the engine's inputs and last activity."""
    state = request.app.state
    journal, config = state.journal, state.config
    last_candles = {
        s: journal.get_state(f"engine:last_candle:{s}") for s in config.paper.symbols
    }
    latest_news = journal.news_page(limit=1)
    day_ago = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="milliseconds")
    return {
        "reconciliation": state.reconciliation,
        "trading_enabled": state.trading_enabled,
        "last_candle_processed": max((v for v in last_candles.values() if v), default=None),
        "news_last_seen": latest_news[0]["seen_at"] if latest_news else None,
        "news_items_24h": len(journal.news_since(day_ago)),
        "orders_last_24h": journal.orders_created_since(day_ago),
    }
