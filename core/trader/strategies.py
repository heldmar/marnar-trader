"""Sprint 4 strategy candidates + D-23 numeric news rules.

Three archetypes go head-to-head in the S4 backtests (PROJECT-PLAN: scalp vs
momentum vs intraday) so the trade-rhythm recommendation (Q9/D-11) is decided
by data, not taste. All are long-only spot (D-10), emit intents through the
``StrategyContext`` socket, and are deterministic — no randomness, no clock.

Candidates:
- ``RsiMeanReversion`` — scalp archetype: buy short-timeframe oversold dips,
  take small profits fast. Lives or dies by fees (many round trips).
- ``DonchianBreakout`` — momentum archetype: buy strength (N-bar high
  breakout), ride it, exit on an M-bar low. Fewer, larger trades.
- ``TrendFollow`` — intraday-trend archetype: EMA cross entries gated by a
  long trend filter; middle ground on trade frequency.

D-23 rules (each backtest-gated, brain uses numeric derivatives only):
- ``EventBlackout`` — approved: no *new* entries inside a window around
  scheduled macro events (FOMC, CPI, ...). Exits and protective stops are
  never blocked.
- ``NewsSpikePause`` — conditional on historical news-count data: pause new
  entries when the per-candle news count spikes above a rolling baseline.

Both rules are strategy *wrappers*: they gate what the inner strategy may do
without the inner strategy knowing they exist — exactly how the future
advisory layer must behave (D-22: restrict-only).
"""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
from collections.abc import Mapping, Sequence

from trader.backtest import Strategy, StrategyContext
from trader.marketdata import Candle

MINUTE_MS = 60_000


class RsiMeanReversion:
    """Scalp candidate: buy when Wilder RSI drops below ``oversold``, exit on
    recovery above ``exit_rsi`` or via tight protective stops. Designed for
    1m–5m candles; the S4 report shows whether fees eat the edge (they did for
    untuned SmaCross in the S3 smoke test)."""

    warmup = 0  # indicator state accumulates internally; guards below

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        exit_rsi: float = 55.0,
        *,
        spend_usdt: float = 15.0,  # 10% of pilot capital (D-08)
        stop_loss_pct: float = 0.7,
        take_profit_pct: float = 1.2,
    ):
        if period < 2:
            raise ValueError("RSI period must be >= 2")
        self.period = period
        self.oversold, self.exit_rsi = oversold, exit_rsi
        self.spend = spend_usdt
        self.sl, self.tp = stop_loss_pct, take_profit_pct
        self._prev_close: float | None = None
        self._avg_gain: float | None = None  # Wilder-smoothed
        self._avg_loss: float | None = None
        self._seed_gains: list[float] = []
        self._seed_losses: list[float] = []

    @property
    def rsi(self) -> float | None:
        if self._avg_gain is None or self._avg_loss is None:
            return None
        if self._avg_loss == 0.0:
            return 100.0
        rs = self._avg_gain / self._avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def _update(self, close: float) -> None:
        if self._prev_close is None:
            self._prev_close = close
            return
        change = close - self._prev_close
        self._prev_close = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self._avg_gain is None:
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)
            if len(self._seed_gains) == self.period:
                self._avg_gain = sum(self._seed_gains) / self.period
                self._avg_loss = sum(self._seed_losses) / self.period
            return
        assert self._avg_loss is not None
        self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
        self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> None:
        self._update(candle.close)
        rsi = self.rsi
        if rsi is None:
            return
        if ctx.position.qty == 0 and rsi < self.oversold:
            ctx.buy(self.spend, stop_loss_pct=self.sl, take_profit_pct=self.tp)
        elif ctx.position.qty > 0 and rsi > self.exit_rsi:
            ctx.sell_all()


class DonchianBreakout:
    """Momentum candidate: buy when the close breaks above the highest high of
    the previous ``entry_n`` candles; exit when it closes below the lowest low
    of the previous ``exit_n``. Protective stop still attached (no-UPS
    discipline: the exchange must hold the stop, not our loop)."""

    warmup = 0

    def __init__(
        self,
        entry_n: int = 20,
        exit_n: int = 10,
        *,
        spend_usdt: float = 15.0,
        stop_loss_pct: float = 2.0,
        take_profit_pct: float | None = None,  # momentum runs — no cap by default
    ):
        if entry_n < 2 or exit_n < 2:
            raise ValueError("channel lengths must be >= 2")
        self.entry_n, self.exit_n = entry_n, exit_n
        self.spend = spend_usdt
        self.sl, self.tp = stop_loss_pct, take_profit_pct
        self._highs: deque[float] = deque(maxlen=entry_n)
        self._lows: deque[float] = deque(maxlen=exit_n)
        # Why the last signal fired (rule + numbers) — journaled by the live
        # engine for the UI trade timeline (D-23 truthful attribution).
        self.last_signal: dict | None = None

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> None:
        entry_ready = len(self._highs) == self.entry_n
        exit_ready = len(self._lows) == self.exit_n
        upper = max(self._highs) if entry_ready else None
        lower = min(self._lows) if exit_ready else None
        # Channels are computed *before* appending: the breakout must clear the
        # prior N candles, not a channel that already contains this candle.
        self._highs.append(candle.high)
        self._lows.append(candle.low)
        if ctx.position.qty == 0 and upper is not None and candle.close > upper:
            self.last_signal = {
                "rule": f"close broke above the {self.entry_n}-candle high",
                "close": candle.close, "level": upper, "n": self.entry_n,
                "candle_open_time": candle.open_time,
            }
            ctx.buy(self.spend, stop_loss_pct=self.sl, take_profit_pct=self.tp)
        elif ctx.position.qty > 0 and lower is not None and candle.close < lower:
            self.last_signal = {
                "rule": f"close fell below the {self.exit_n}-candle low",
                "close": candle.close, "level": lower, "n": self.exit_n,
                "candle_open_time": candle.open_time,
            }
            ctx.sell_all()


class TrendFollow:
    """Intraday-trend candidate: fast/slow EMA cross entries, taken only while
    the close sits above a long trend EMA (no knife-catching in downtrends);
    exit on the reverse cross."""

    warmup = 0

    def __init__(
        self,
        fast: int = 12,
        slow: int = 48,
        trend: int = 200,
        *,
        spend_usdt: float = 15.0,
        stop_loss_pct: float = 1.5,
        take_profit_pct: float | None = 3.0,
    ):
        if not fast < slow < trend:
            raise ValueError("periods must satisfy fast < slow < trend")
        self.periods = (fast, slow, trend)
        self.spend = spend_usdt
        self.sl, self.tp = stop_loss_pct, take_profit_pct
        self._emas: list[float | None] = [None, None, None]
        self._count = 0
        self._prev_diff: float | None = None

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> None:
        close = candle.close
        for i, n in enumerate(self.periods):
            prev = self._emas[i]
            alpha = 2.0 / (n + 1.0)
            self._emas[i] = close if prev is None else prev + alpha * (close - prev)
        self._count += 1
        fast_e, slow_e, trend_e = self._emas
        assert fast_e is not None and slow_e is not None and trend_e is not None
        diff = fast_e - slow_e
        prev_diff, self._prev_diff = self._prev_diff, diff
        if self._count <= self.periods[2] or prev_diff is None:
            return  # EMAs not yet representative — no signals during seeding
        crossed_up = prev_diff <= 0 < diff
        crossed_down = prev_diff >= 0 > diff
        if ctx.position.qty == 0 and crossed_up and close > trend_e:
            ctx.buy(self.spend, stop_loss_pct=self.sl, take_profit_pct=self.tp)
        elif ctx.position.qty > 0 and crossed_down:
            ctx.sell_all()


class _EntryGatedContext:
    """Context proxy that drops buy intents while a rule says 'no new risk'.
    Sells always pass — a restriction layer must never trap us in a position
    (D-22: restrict-only means restrict *entries*)."""

    def __init__(self, inner: StrategyContext):
        self._inner = inner

    @property
    def position(self):
        return self._inner.position

    @property
    def cash(self) -> float:
        return self._inner.cash

    def buy(self, quote_amount: float, **kwargs) -> None:
        return None  # gated

    def sell_all(self) -> None:
        self._inner.sell_all()


class EventBlackout:
    """D-23 approved rule: block new entries from ``before_min`` before to
    ``after_min`` after each scheduled event (epoch-ms timestamps: FOMC
    statements, CPI releases, ...). Numeric input only — no text, no AI."""

    def __init__(
        self,
        inner: Strategy,
        event_times_ms: Sequence[int],
        *,
        before_min: int = 60,
        after_min: int = 60,
    ):
        self.inner = inner
        self.warmup = inner.warmup
        self.events = sorted(event_times_ms)
        self.before_ms = before_min * MINUTE_MS
        self.after_ms = after_min * MINUTE_MS
        self.blocked_candles = 0  # reported in the S4 backtest write-up

    def in_blackout(self, t_ms: int) -> bool:
        i = bisect_left(self.events, t_ms)
        for ev in self.events[max(0, i - 1) : i + 1]:
            if ev - self.before_ms <= t_ms <= ev + self.after_ms:
                return True
        return False

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> None:
        if self.in_blackout(candle.open_time):
            self.blocked_candles += 1
            self.inner.on_candle(candle, _EntryGatedContext(ctx))  # type: ignore[arg-type]
        else:
            self.inner.on_candle(candle, ctx)


class NewsSpikePause:
    """D-23 conditional rule: given per-candle news counts (open_time -> count,
    from whatever historical source S4 can obtain), pause new entries for
    ``pause_min`` once the count exceeds ``spike_mult`` x the rolling mean of
    the last ``baseline_candles``. Candles missing from *counts* count as 0."""

    def __init__(
        self,
        inner: Strategy,
        counts: Mapping[int, int],
        *,
        baseline_candles: int = 288,  # one day of 5m candles
        spike_mult: float = 3.0,
        min_spike_count: int = 5,  # a 0->2 jump is noise, not news
        pause_min: int = 60,
    ):
        self.inner = inner
        self.warmup = inner.warmup
        self.counts = counts
        self.spike_mult = spike_mult
        self.min_spike_count = min_spike_count
        self.pause_ms = pause_min * MINUTE_MS
        self._window: deque[int] = deque(maxlen=baseline_candles)
        self._paused_until: int | None = None
        self.blocked_candles = 0

    def on_candle(self, candle: Candle, ctx: StrategyContext) -> None:
        count = self.counts.get(candle.open_time, 0)
        baseline = sum(self._window) / len(self._window) if self._window else 0.0
        self._window.append(count)
        if count >= self.min_spike_count and count > self.spike_mult * baseline:
            self._paused_until = candle.open_time + self.pause_ms
        paused = self._paused_until is not None and candle.open_time < self._paused_until
        if paused:
            self.blocked_candles += 1
            self.inner.on_candle(candle, _EntryGatedContext(ctx))  # type: ignore[arg-type]
        else:
            self.inner.on_candle(candle, ctx)
