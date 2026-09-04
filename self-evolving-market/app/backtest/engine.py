"""§7.3 백테스트 엔진 — 자체 구현(룩어헤드 통제), 이벤트 기반 일봉.

절대 규칙
  - 신호 t 종가 → 체결 t+1 시가. 예외 없음 (§3 설계 원칙 5).
  - 시가 결측 또는 다음 바가 5일 넘게 뒤면 신호 폐기.
  - 비용은 필수 인자. CostModel 없이는 엔진이 만들어지지 않는다 (#3).
  - 하드 제약은 전략이 우회할 수 없다 (#19 LEV/INV 5일, #20 SHORT 손절 -8%, #21 익스포저 150%).
  - 회계 통화는 원화. USD 자산은 그날의 PIT 환율로 환산한다 (#10 환율 혼합 방지).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.costs import CostModel
from app.config import risk as load_risk
from app.features.builder import FeaturePanel
from app.strategies.base import Strategy
from app.universe.snapshot import symbol_meta
from app.util.hashing import hash_obj

MAX_FILL_GAP_DAYS = 5
BASE_CCY = "KRW"


# ---------------------------------------------------------------- 가격 조회


class PriceBook:
    """심볼별 OHLCV 를 numpy 배열로 들고 O(log n) 조회한다."""

    __slots__ = ("_by_symbol", "_fx", "_fx_dates", "timeline")

    def __init__(self, prices: pd.DataFrame, fx: pd.DataFrame | None = None):
        self._by_symbol: dict[str, dict[str, np.ndarray]] = {}
        for sym, g in prices.groupby("symbol", sort=True):
            g = g.sort_values("event_date")
            af = g["adj_factor"].fillna(1.0).to_numpy(float)
            self._by_symbol[str(sym)] = {
                "d": g["event_date"].to_numpy("datetime64[D]").astype("int64"),
                "o": g["open"].to_numpy(float),
                "h": g["high"].to_numpy(float),
                "l": g["low"].to_numpy(float),
                "c": g["close"].to_numpy(float),
                "v": g["volume"].to_numpy(float),
                "af": af,
            }
        self.timeline = np.array(
            sorted({int(x) for s in self._by_symbol.values() for x in s["d"]}), dtype="int64"
        )
        if fx is not None and not fx.empty:
            f = fx.sort_values("event_date")
            self._fx_dates = f["event_date"].to_numpy("datetime64[D]").astype("int64")
            self._fx = f["rate"].to_numpy(float)
        else:
            self._fx_dates = np.array([], dtype="int64")
            self._fx = np.array([], dtype=float)

    @staticmethod
    def to_ord(d: dt.date | pd.Timestamp) -> int:
        return int(np.datetime64(pd.Timestamp(d).date(), "D").astype("int64"))

    @staticmethod
    def from_ord(o: int) -> dt.date:
        return np.datetime64(int(o), "D").astype(dt.date)

    def symbols(self) -> list[str]:
        return sorted(self._by_symbol)

    def bar(self, symbol: str, day_ord: int) -> dict[str, float] | None:
        s = self._by_symbol.get(symbol)
        if s is None:
            return None
        i = int(np.searchsorted(s["d"], day_ord))
        if i >= len(s["d"]) or s["d"][i] != day_ord:
            return None
        return {k: float(s[k][i]) for k in ("o", "h", "l", "c", "v", "af")}

    def next_bar(self, symbol: str, after_ord: int) -> tuple[int, dict[str, float]] | None:
        """after_ord **이후** 첫 바. 이것이 t+1 체결의 정의다."""
        s = self._by_symbol.get(symbol)
        if s is None:
            return None
        i = int(np.searchsorted(s["d"], after_ord, side="right"))
        if i >= len(s["d"]):
            return None
        d = int(s["d"][i])
        return d, {k: float(s[k][i]) for k in ("o", "h", "l", "c", "v", "af")}

    def last_close(self, symbol: str, day_ord: int) -> float | None:
        s = self._by_symbol.get(symbol)
        if s is None:
            return None
        i = int(np.searchsorted(s["d"], day_ord, side="right")) - 1
        return float(s["c"][i]) if i >= 0 else None

    def adv20(self, symbol: str, day_ord: int) -> float | None:
        s = self._by_symbol.get(symbol)
        if s is None:
            return None
        i = int(np.searchsorted(s["d"], day_ord, side="right"))
        lo = max(0, i - 20)
        if i <= lo:
            return None
        return float(np.mean(s["c"][lo:i] * s["v"][lo:i]))

    def fx_rate(self, day_ord: int) -> float:
        """USDKRW. 결측은 전일 이월 (§4.3)."""
        if len(self._fx) == 0:
            return 1300.0
        i = int(np.searchsorted(self._fx_dates, day_ord, side="right")) - 1
        return float(self._fx[max(i, 0)])


# ---------------------------------------------------------------- 상태


@dataclass
class Position:
    symbol: str
    market: str
    kind: str
    side: int                 # +1 롱, -1 숏
    qty: float
    entry_px: float
    entry_ord: int
    leverage: float
    stop_px: float
    horizon_due_ord: int
    hard_due_ord: int | None
    entry_cost_base: float
    carry_base: float = 0.0
    strategy_id: str = ""
    family: str = ""
    exit_pending: bool = False

    @property
    def is_short(self) -> bool:
        return self.side < 0


@dataclass
class PendingOrder:
    symbol: str
    side: int                 # 주문 방향 (+1 매수, -1 매도)
    action: str               # "open" | "close"
    signal_ord: int
    qty: float | None = None
    reason: str = ""
    target_weight: float = 0.0


@dataclass
class BacktestResult:
    strategy_id: str
    account: str
    trades: pd.DataFrame
    nav: pd.DataFrame
    metrics: dict[str, Any]
    code_hash: str
    data_hash: str
    gates_hash: str
    costs_enabled: bool = True
    warnings: list[str] = field(default_factory=list)

    @property
    def closed_trades(self) -> pd.DataFrame:
        if self.trades.empty:
            return self.trades
        return self.trades[self.trades["closed"] == 1]


# ---------------------------------------------------------------- 엔진


class BacktestEngine:
    def __init__(
        self,
        panel: FeaturePanel,
        book: PriceBook,
        cost_model: CostModel,          # ★ 필수. 기본값 없음 (#3)
        *,
        account: str = "ACC_L",
        risk_cfg: dict | None = None,
        seed: int = 0,
    ):
        if cost_model is None:
            raise ValueError("CostModel 은 필수 인자입니다. 비용 없는 백테스트는 허용되지 않습니다 (#3).")
        self.panel = panel
        self.book = book
        self.costs = cost_model
        self.account = account
        self.risk = risk_cfg or load_risk()
        self.seed = seed
        self.meta = symbol_meta()

        acc = self.risk["accounts"][account]
        self.capital = float(acc["capital_krw"])
        self.position_limit = float(acc["position_limit_pct"])
        self.max_positions = acc.get("max_positions")
        self.min_shares = float(acc.get("min_shares", 1))
        lim = self.risk["limits"]
        self.cash_min = float(lim["cash_min_pct"])
        self.gross_max = float(lim["gross_notional_max_pct"])
        self.short_max = float(lim["short_notional_max_pct"])
        self.levinv_max = float(lim["lev_inv_notional_max_pct"])
        self.hard = self.risk["hard_constraints"]
        self.stops = self.risk["stop_loss"]

    # ------------------------------------------------------------ 제약 조회

    def stop_pct(self, family: str, horizon: int) -> float:
        cfg = self.stops.get(family, {})
        if "default" in cfg:
            return float(cfg["default"])
        key = f"h{horizon}"
        if key in cfg:
            return float(cfg[key])
        # 호라이즌이 표에 없으면 가장 가까운 큰 호라이즌의 값을 쓴다.
        keys = sorted(((int(k[1:]), v) for k, v in cfg.items() if k.startswith("h")), key=lambda kv: kv[0])
        for h, v in keys:
            if horizon <= h:
                return float(v)
        return float(keys[-1][1]) if keys else 0.15

    def max_hold_days(self, family: str) -> int | None:
        v = (self.hard.get("max_hold_days") or {}).get(family)
        return int(v) if v is not None else None

    def allowed_regimes(self, family: str) -> tuple[str, ...] | None:
        v = (self.hard.get("regime_required") or {}).get(family)
        return tuple(v) if v else None

    def enforce_horizon(self, family: str, horizon: int) -> int:
        """#19 LEV/INV 는 호라이즌 자체를 5일로 강제한다."""
        cap = (self.hard.get("max_horizon_days") or {}).get(family)
        return min(horizon, int(cap)) if cap is not None else horizon

    # ------------------------------------------------------------ 실행

    def run(
        self,
        strategy: Strategy,
        start: dt.date,
        end: dt.date,
        *,
        gates_hash: str = "",
        code_hash: str = "",
        data_hash: str = "",
    ) -> BacktestResult:
        b, costs = self.book, self.costs
        family = str(strategy.family)
        horizon = self.enforce_horizon(family, int(strategy.horizon_days))
        stop_pct = self.stop_pct(family, horizon)
        max_hold = self.max_hold_days(family)
        regimes_ok = self.allowed_regimes(family)
        htb = costs.hard_to_borrow()
        warnings: list[str] = []

        s_ord, e_ord = PriceBook.to_ord(start), PriceBook.to_ord(end)
        timeline = [int(d) for d in b.timeline if s_ord <= d <= e_ord]
        if not timeline:
            return self._empty_result(strategy, code_hash, data_hash, gates_hash, ["구간에 거래일이 없습니다."])

        cash = self.capital
        positions: dict[str, Position] = {}
        pending: list[PendingOrder] = []
        trades: list[dict] = []
        nav_rows: list[dict] = []
        peak = self.capital
        trade_no = 0

        for day in timeline:
            fx = b.fx_rate(day)

            def to_base(amount: float, market: str, _fx: float = fx) -> float:
                return amount * (_fx if market == "US" else 1.0)

            # ---------------------------------------- A. pending 체결 (당일 시가)
            still: list[PendingOrder] = []
            for order in pending:
                bar = b.bar(order.symbol, day)
                tradable = bar is not None and np.isfinite(bar["o"]) and bar["o"] > 0
                if not tradable:
                    # 청산 주문은 폐기할 수 없다. 거래 가능한 첫 바까지 들고 간다.
                    if order.action == "close":
                        still.append(order)
                    else:
                        warnings.append(f"{PriceBook.from_ord(day)} {order.symbol}: 시가 결측 → 신호 폐기 (§7.3)")
                    continue
                if order.action == "open" and day - order.signal_ord > MAX_FILL_GAP_DAYS:
                    warnings.append(f"{order.symbol}: 체결 지연 {day - order.signal_ord}일 → 신호 폐기")
                    continue

                m = self.meta.get(order.symbol)
                market = m.market if m else "US"
                kind = m.kind if m else "stock"

                if order.action == "close":
                    pos = positions.get(order.symbol)
                    if pos is None:
                        continue
                    cash = self._close(
                        pos, bar["o"], day, order.reason, cash, fx, trades, trade_no := trade_no + 1
                    )
                    positions.pop(order.symbol, None)
                    continue

                # 신규 진입
                if order.symbol in positions:
                    continue
                nav_now = self._nav(cash, positions, day, fx)
                qty = self._size(order, bar["o"], nav_now, market, kind, fx)
                if qty <= 0:
                    continue
                if not self._exposure_ok(positions, order, qty, bar["o"], nav_now, kind, market, fx, day):
                    warnings.append(f"{PriceBook.from_ord(day)} {order.symbol}: 익스포저 상한으로 주문 축소/폐기")
                    continue

                is_short = order.side < 0
                fill = costs.fill(
                    ref_price=bar["o"], qty=qty, side=order.side, opening=True,
                    market=market, kind=kind, is_short=is_short, adv20=b.adv20(order.symbol, day),
                )
                notional_local = qty * fill.price
                cost_base = to_base(fill.cost, market)
                # 숏은 진입 시 현금이 들어오고 청산 시 나간다.
                cash += (-to_base(notional_local, market) if not is_short else to_base(notional_local, market))
                cash -= cost_base

                lev = abs(float(m.leverage)) if m else 1.0
                stop_px = fill.price * (1 - stop_pct) if not is_short else fill.price * (1 + stop_pct)
                hard_due = None
                if max_hold is not None:
                    hard_due = self._due_ord(order.symbol, day, max_hold)
                positions[order.symbol] = Position(
                    symbol=order.symbol, market=market, kind=kind, side=order.side, qty=qty,
                    entry_px=fill.price, entry_ord=day, leverage=lev, stop_px=stop_px,
                    horizon_due_ord=self._due_ord(order.symbol, day, horizon),
                    hard_due_ord=hard_due, entry_cost_base=cost_base,
                    strategy_id=strategy.id, family=family,
                )
            pending = still

            # ---------------------------------------- B. 장중 손절 / 상폐 (#20, §7.3)
            for sym in list(positions):
                pos = positions[sym]
                bar = b.bar(sym, day)
                if bar is None:
                    continue
                meta = self.meta.get(sym)
                if meta and meta.delist_date and PriceBook.to_ord(meta.delist_date) <= day:
                    last = b.last_close(sym, day) or bar["c"]
                    ratio = float(costs.delisting.get("forced_exit_price_ratio", 0.5))
                    px = last * (ratio if not pos.is_short else 1.0)
                    cash = self._close(pos, px, day, "delist", cash, fx, trades, trade_no := trade_no + 1)
                    positions.pop(sym, None)
                    continue
                hit = (not pos.is_short and bar["l"] <= pos.stop_px) or (pos.is_short and bar["h"] >= pos.stop_px)
                if hit and day > pos.entry_ord:
                    # 갭이 손절가를 뛰어넘으면 시가에 체결된다 (낙관 금지).
                    px = min(bar["o"], pos.stop_px) if not pos.is_short else max(bar["o"], pos.stop_px)
                    cash = self._close(pos, px, day, "stop_loss", cash, fx, trades, trade_no := trade_no + 1)
                    positions.pop(sym, None)

            # ---------------------------------------- C. 보유 비용 (대차·운용보수)
            for pos in positions.values():
                px = b.last_close(pos.symbol, day) or pos.entry_px
                carry_local = costs.carry(
                    notional=pos.qty * px, kind=pos.kind, is_short=pos.is_short, days=1
                )
                carry_base = to_base(carry_local, pos.market)
                pos.carry_base += carry_base
                cash -= carry_base

            # ---------------------------------------- D. 만기 청산 예약 (t+1 시가)
            for sym, pos in positions.items():
                if pos.exit_pending:
                    continue
                due = pos.horizon_due_ord
                reason = "horizon"
                if pos.hard_due_ord is not None and pos.hard_due_ord < due:
                    due, reason = pos.hard_due_ord, "max_hold"
                if day >= due:
                    pos.exit_pending = True
                    pending.append(PendingOrder(sym, -pos.side, "close", day, reason=reason))

            # ---------------------------------------- E. 신호 → 진입 예약 (t+1 시가)
            snap = self.panel.on(PriceBook.from_ord(day))
            if not snap.empty:
                regime_now = str(snap["regime"].iloc[0]) if "regime" in snap.columns else "sideways"
                gate_open = regimes_ok is None or regime_now in regimes_ok
                if gate_open:
                    sig = strategy.signals(snap, PriceBook.from_ord(day))
                    queued = {o.symbol for o in pending}
                    sig = self._filter_signals(sig, family, htb, positions, snap, queued)
                    n = len(sig)
                    if n:
                        weight = min(self.position_limit, max(0.0, (1 - self.cash_min)) / n)
                        for _, row in sig.iterrows():
                            pending.append(
                                PendingOrder(
                                    symbol=str(row["symbol"]), side=int(row["side"]), action="open",
                                    signal_ord=day, target_weight=weight, reason="signal",
                                )
                            )

            # ---------------------------------------- F. NAV 기록
            nav = self._nav(cash, positions, day, fx)
            peak = max(peak, nav)
            exp = self._exposures(positions, day, nav, fx)
            nav_rows.append(
                {
                    "date": PriceBook.from_ord(day),
                    "nav": nav,
                    "cash": cash,
                    "drawdown": (nav / peak - 1.0) if peak > 0 else 0.0,
                    "n_positions": len(positions),
                    **exp,
                }
            )

        # 구간 종료: 미청산 포지션은 마지막 종가로 평가만 하고 **거래로 세지 않는다** (#12 승률 정의).
        for sym, pos in positions.items():
            px = self.book.last_close(sym, timeline[-1]) or pos.entry_px
            trades.append(self._trade_row(pos, px, timeline[-1], "open_at_end", closed=False,
                                          fx=self.book.fx_rate(timeline[-1]), trade_no=(trade_no := trade_no + 1)))

        tdf = pd.DataFrame(trades)
        ndf = pd.DataFrame(nav_rows)
        metrics = compute_metrics(tdf, ndf, self.capital)
        metrics["family"] = family
        metrics["horizon_days"] = horizon
        return BacktestResult(
            strategy_id=strategy.id, account=self.account, trades=tdf, nav=ndf, metrics=metrics,
            code_hash=code_hash or hash_obj(strategy.describe() if hasattr(strategy, "describe") else strategy.id),
            data_hash=data_hash, gates_hash=gates_hash, costs_enabled=self.costs.enabled,
            warnings=warnings[:200],
        )

    # ------------------------------------------------------------ 보조

    def _offset_ord(self, symbol: str, day_ord: int, n_bars: int) -> int:
        """그 심볼의 거래일 기준 n번째 뒤 바의 날짜. n=0 이면 당일."""
        s = self.book._by_symbol.get(symbol)  # noqa: SLF001 - 같은 모듈 내 최적화 경로
        if s is None:
            return day_ord + n_bars
        i = int(np.searchsorted(s["d"], day_ord))
        j = min(i + max(0, n_bars), len(s["d"]) - 1)
        return int(s["d"][j])

    def _due_ord(self, symbol: str, day_ord: int, hold_bars: int) -> int:
        """정확히 hold_bars 거래일 보유하려면 그 하루 **전**에 청산 주문을 낸다.

        청산 주문은 t+1 시가에 체결되므로, bar(hold_bars-1) 에 예약해야 bar(hold_bars) 에 나간다.
        이 한 칸을 빼먹으면 LEV/INV 가 6거래일 보유되어 #19 를 위반한다.
        """
        return self._offset_ord(symbol, day_ord, max(0, int(hold_bars) - 1))

    def _bars_between(self, symbol: str, a_ord: int, b_ord: int) -> int:
        s = self.book._by_symbol.get(symbol)  # noqa: SLF001
        if s is None:
            return max(0, b_ord - a_ord)
        i = int(np.searchsorted(s["d"], a_ord))
        j = int(np.searchsorted(s["d"], b_ord))
        return max(0, j - i)

    def _filter_signals(
        self, sig: pd.DataFrame, family: str, htb: frozenset[str],
        positions: dict[str, Position], snap: pd.DataFrame, queued: set[str],
    ) -> pd.DataFrame:
        if sig.empty:
            return sig
        out = sig[~sig["symbol"].isin(set(positions) | queued)]
        if family == "SHORT_US":
            out = out[~out["symbol"].isin(htb)]                       # hard-to-borrow 제외
            floor = float(self.hard.get("short_us", {}).get("exclude_mcap_bottom_pct", 0.0))
            if floor > 0 and "adv20" in snap.columns:
                rank = snap["adv20"].rank(pct=True)
                keep = set(rank[rank > floor].index.astype(str))
                out = out[out["symbol"].isin(keep)]                   # 시총 하위 제외 (거래대금 대용)
        if self.max_positions:
            room = int(self.max_positions) - len(positions)
            out = out.head(max(0, room))
        return out

    def _size(self, order: PendingOrder, price: float, nav: float, market: str, kind: str, fx: float) -> float:
        if price <= 0 or nav <= 0:
            return 0.0
        lev = abs(float(self.meta[order.symbol].leverage)) if order.symbol in self.meta else 1.0
        # 명목 기준으로 목표 비중을 잡는다 → 2배 ETF 10% 는 명목 20% (§8.2)
        target_base = nav * order.target_weight / max(lev, 1.0)
        target_local = target_base / (fx if market == "US" else 1.0)
        qty = target_local / price
        frac, min_frac = self.costs.fractional(market)
        qty = np.floor(qty / min_frac) * min_frac if frac else float(np.floor(qty))
        if not frac and qty < self.min_shares:
            # ACC_S 정수 주식 제약: 1주도 못 사면 주문 폐기 (#24 가 이 왜곡을 리포트한다)
            return 0.0
        return max(qty, 0.0)

    def _exposure_ok(
        self, positions: dict[str, Position], order: PendingOrder, qty: float, price: float,
        nav: float, kind: str, market: str, fx: float, day: int,
    ) -> bool:
        """#21 명목 익스포저 상한. 초과 주문은 폐기하고 사유를 남긴다."""
        if nav <= 0:
            return False
        lev = abs(float(self.meta[order.symbol].leverage)) if order.symbol in self.meta else 1.0
        add_base = qty * price * (fx if market == "US" else 1.0) * lev
        cur = self._exposures(positions, day, nav, fx)
        gross = cur["gross_notional_pct"] + add_base / nav
        if gross > self.gross_max:
            return False
        if order.side < 0 and cur["short_notional_pct"] + add_base / nav > self.short_max:
            return False
        return not (
            kind in ("lev_etf", "inv_etf")
            and cur["lev_inv_notional_pct"] + add_base / nav > self.levinv_max
        )

    def _exposures(self, positions: dict[str, Position], day: int, nav: float, fx: float) -> dict[str, float]:
        gross = short = levinv = 0.0
        for pos in positions.values():
            px = self.book.last_close(pos.symbol, day) or pos.entry_px
            base = pos.qty * px * (fx if pos.market == "US" else 1.0) * pos.leverage
            gross += base
            if pos.is_short:
                short += base
            if pos.kind in ("lev_etf", "inv_etf"):
                levinv += base
        denom = nav if nav > 0 else 1.0
        return {
            "gross_notional_pct": gross / denom,
            "short_notional_pct": short / denom,
            "lev_inv_notional_pct": levinv / denom,
        }

    def _nav(self, cash: float, positions: dict[str, Position], day: int, fx: float) -> float:
        total = cash
        for pos in positions.values():
            px = self.book.last_close(pos.symbol, day) or pos.entry_px
            mv = pos.qty * px * (fx if pos.market == "US" else 1.0)
            total += mv if not pos.is_short else -mv
        return total

    def _close(
        self, pos: Position, ref_px: float, day: int, reason: str, cash: float,
        fx: float, trades: list[dict], trade_no: int,
    ) -> float:
        side = -pos.side
        fill = self.costs.fill(
            ref_price=ref_px, qty=pos.qty, side=side, opening=False, market=pos.market,
            kind=pos.kind, is_short=pos.is_short, adv20=self.book.adv20(pos.symbol, day),
        )
        rate = fx if pos.market == "US" else 1.0
        proceeds = pos.qty * fill.price * rate
        cash += (proceeds if not pos.is_short else -proceeds) - fill.cost * rate
        trades.append(self._trade_row(pos, fill.price, day, reason, closed=True, fx=fx,
                                      trade_no=trade_no, exit_cost_base=fill.cost * rate))
        return cash

    def _trade_row(
        self, pos: Position, exit_px: float, day: int, reason: str, *, closed: bool,
        fx: float, trade_no: int, exit_cost_base: float = 0.0,
    ) -> dict:
        rate = fx if pos.market == "US" else 1.0
        entry_notional_base = pos.qty * pos.entry_px * rate
        gross = pos.qty * (exit_px - pos.entry_px) * rate * pos.side
        total_cost = pos.entry_cost_base + exit_cost_base + pos.carry_base
        net = gross - total_cost
        return {
            "trade_id": f"{pos.strategy_id}-{pos.symbol}-{PriceBook.from_ord(pos.entry_ord)}-{trade_no}",
            "strategy_id": pos.strategy_id,
            "family": pos.family,
            "symbol": pos.symbol,
            "market": pos.market,
            "instrument": pos.kind,
            "side": pos.side,
            "signal_date": PriceBook.from_ord(pos.entry_ord),
            "fill_date": PriceBook.from_ord(pos.entry_ord),
            "exit_date": PriceBook.from_ord(day),
            "fill_px": pos.entry_px,
            "exit_px": exit_px,
            "qty": pos.qty,
            "cost": pos.entry_cost_base + exit_cost_base,
            "borrow_cost": pos.carry_base,
            "gross_pnl": gross,
            "pnl": net,
            "pnl_pct": net / entry_notional_base if entry_notional_base else 0.0,
            "hold_days": int(day - pos.entry_ord),
            "hold_bars": self._bars_between(pos.symbol, pos.entry_ord, day),
            "closed": 1 if closed else 0,
            "exit_reason": reason,
        }

    def _empty_result(self, strategy, code_hash, data_hash, gates_hash, warnings) -> BacktestResult:
        return BacktestResult(
            strategy_id=strategy.id, account=self.account, trades=pd.DataFrame(), nav=pd.DataFrame(),
            metrics=compute_metrics(pd.DataFrame(), pd.DataFrame(), self.capital),
            code_hash=code_hash, data_hash=data_hash, gates_hash=gates_hash,
            costs_enabled=self.costs.enabled, warnings=warnings,
        )


# ---------------------------------------------------------------- 지표


def compute_metrics(trades: pd.DataFrame, nav: pd.DataFrame, capital: float) -> dict[str, Any]:
    """§1.2 지표. 승률은 **청산 거래만** 센다 (#12)."""
    out: dict[str, Any] = {
        "n_trades": 0, "n_closed": 0, "win_rate": np.nan, "expectancy": np.nan,
        "avg_win": np.nan, "avg_loss": np.nan, "sharpe": np.nan, "mdd": 0.0,
        "cagr": np.nan, "total_return": np.nan, "worst_trade": np.nan,
        "gross_exposure_max": 0.0, "avg_hold_days": np.nan,
    }
    if trades is not None and not trades.empty:
        closed = trades[trades["closed"] == 1]
        out["n_trades"] = int(len(trades))
        out["n_closed"] = int(len(closed))
        if not closed.empty:
            r = closed["pnl_pct"].astype(float)
            wins, losses = r[r > 0], r[r <= 0]
            out["win_rate"] = float((r > 0).mean())
            out["expectancy"] = float(r.mean())
            out["avg_win"] = float(wins.mean()) if len(wins) else 0.0
            out["avg_loss"] = float(losses.mean()) if len(losses) else 0.0
            out["worst_trade"] = float(r.min())
            out["avg_hold_days"] = float(closed["hold_days"].mean())

    if nav is not None and not nav.empty:
        v = nav["nav"].astype(float).to_numpy()
        ret = np.diff(v) / v[:-1] if len(v) > 1 else np.array([0.0])
        ret = ret[np.isfinite(ret)]
        if len(ret) > 1 and ret.std(ddof=1) > 0:
            out["sharpe"] = float(ret.mean() / ret.std(ddof=1) * np.sqrt(252))
        out["mdd"] = float(-nav["drawdown"].min()) if "drawdown" in nav else 0.0
        out["total_return"] = float(v[-1] / capital - 1.0)
        years = max(len(v) / 252.0, 1e-9)
        out["cagr"] = float((v[-1] / capital) ** (1 / years) - 1.0) if v[-1] > 0 else -1.0
        if "gross_notional_pct" in nav:
            out["gross_exposure_max"] = float(nav["gross_notional_pct"].max())
    return out
