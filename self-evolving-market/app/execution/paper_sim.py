"""§9 PaperBroker — t+1 시가 체결, §7.3 과 동일한 비용. 두 계좌 병행.

공매도 시뮬 가정을 명시한다: 리콜 없음, 대차 항상 가능.
→ 리포트에 "실전 재현성 낮음" 라벨이 고정으로 붙는다.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.backtest.costs import CostModel
from app.backtest.engine import PriceBook
from app.execution.broker import Fill, Money, Order, Position
from app.paths import state_dir
from app.universe.snapshot import kind_of, leverage_of

SHORT_SIM_DISCLAIMER = "공매도 시뮬 가정: 리콜 없음·대차 항상 가능 → 실전 재현성 낮음"
STATE_FILE = "paper_broker.json"


@dataclass
class _AccountState:
    cash: float
    positions: dict[str, dict] = field(default_factory=dict)


class PaperBroker:
    """상태는 디스크에 남는다(설계 원칙 2). 같은 날 재실행은 같은 결과(#11)."""

    def __init__(
        self,
        book: PriceBook,
        cost_model: CostModel,
        capital: dict[str, float],
        *,
        path: Path | None = None,
    ):
        self.book = book
        self.costs = cost_model
        self.path = path or (state_dir() / STATE_FILE)
        self.state: dict[str, _AccountState] = {}
        self._load(capital)

    # ------------------------------------------------------------ 상태

    def _load(self, capital: dict[str, float]) -> None:
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.state = {
                acc: _AccountState(cash=float(v["cash"]), positions=dict(v.get("positions", {})))
                for acc, v in raw.items()
            }
        for acc, cap in capital.items():
            self.state.setdefault(acc, _AccountState(cash=float(cap)))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({a: asdict(s) for a, s in sorted(self.state.items())},
                       ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------ Broker 계약

    def submit(self, orders: list[Order], as_of: dt.date) -> list[Fill]:
        """as_of = 체결일(= 신호일 t 의 다음 거래일). 그날 **시가**로 체결한다."""
        day = PriceBook.to_ord(as_of)
        fills: list[Fill] = []
        # 결정론: 계좌·심볼 사전순으로 처리한다.
        for order in sorted(orders, key=lambda o: (o.account, o.symbol, o.side)):
            if order.signal_date >= as_of:
                fills.append(Fill(order, as_of, 0.0, 0.0, 0.0, rejected=True,
                                  reject_reason="신호 t → 체결 t+1 위반 (§3 원칙 5)"))
                continue
            bar = self.book.bar(order.symbol, day)
            if bar is None or bar["o"] <= 0:
                fills.append(Fill(order, as_of, 0.0, 0.0, 0.0, rejected=True,
                                  reject_reason="시가 결측 → 신호 폐기"))
                continue
            fills.append(self._execute(order, as_of, day, bar["o"]))
        self.save()
        return fills

    def positions(self, account: str) -> list[Position]:
        st = self.state.get(account)
        if not st:
            return []
        return [
            Position(account=account, symbol=sym, market=p["market"], qty=float(p["qty"]),
                     avg_px=float(p["avg_px"]), strategy_id=p.get("strategy_id", ""),
                     instrument=p.get("instrument", "stock"), leverage=float(p.get("leverage", 1.0)))
            for sym, p in sorted(st.positions.items())
        ]

    def cash(self, account: str) -> Money:
        st = self.state.get(account)
        return Money(float(st.cash) if st else 0.0, "KRW")

    # ------------------------------------------------------------ 내부

    def _execute(self, order: Order, as_of: dt.date, day: int, ref_px: float) -> Fill:
        st = self.state[order.account]
        kind = kind_of(order.symbol)
        held = st.positions.get(order.symbol)
        is_short = (held is not None and float(held["qty"]) < 0) or (held is None and order.side < 0)
        opening = held is None

        f = self.costs.fill(
            ref_price=ref_px, qty=order.qty, side=order.side, opening=opening,
            market=order.market, kind=kind, is_short=is_short,
            adv20=self.book.adv20(order.symbol, day),
        )
        fx = self.book.fx_rate(day) if order.market == "US" else 1.0
        notional_base = order.qty * f.price * fx
        cost_base = f.cost * fx
        st.cash += (-notional_base if order.side > 0 else notional_base) - cost_base

        signed = order.qty * order.side
        if held is None:
            if abs(signed) > 0:
                stop_px = (
                    f.price * (1 - order.stop_pct) if order.side > 0 else f.price * (1 + order.stop_pct)
                ) if order.stop_pct else 0.0
                st.positions[order.symbol] = {
                    "qty": signed, "avg_px": f.price, "market": order.market,
                    "strategy_id": order.strategy_id, "instrument": kind,
                    "leverage": abs(leverage_of(order.symbol)),
                    "entry_date": as_of.isoformat(),
                    "entry_cost_base": cost_base,
                    "family": order.family,
                    "horizon_days": int(order.horizon_days),
                    "stop_px": float(stop_px),
                    "max_hold_days": order.max_hold_days,
                    "carry_base": 0.0,
                }
        else:
            new_qty = float(held["qty"]) + signed
            if abs(new_qty) < 1e-9:
                st.positions.pop(order.symbol, None)
            else:
                if (new_qty > 0) == (float(held["qty"]) > 0) and abs(new_qty) > abs(float(held["qty"])):
                    # 같은 방향 추가 매수 → 평균단가 갱신
                    prev = float(held["qty"])
                    held["avg_px"] = (prev * float(held["avg_px"]) + signed * f.price) / new_qty
                held["qty"] = new_qty
        return Fill(order, as_of, f.price, order.qty, cost_base)

    # ------------------------------------------------------------ 청산·보유비용

    def close(self, account: str, symbol: str, ref_px: float, as_of: dt.date, reason: str) -> dict | None:
        """포지션 전량 청산. 실현된 거래 한 건(dict)을 돌려준다. 이것이 W_trade 의 원천이다."""
        st = self.state.get(account)
        if not st or symbol not in st.positions:
            return None
        p = st.positions[symbol]
        qty = abs(float(p["qty"]))
        pos_side = 1 if float(p["qty"]) > 0 else -1
        is_short = pos_side < 0
        day = PriceBook.to_ord(as_of)

        f = self.costs.fill(
            ref_price=ref_px, qty=qty, side=-pos_side, opening=False, market=p["market"],
            kind=p.get("instrument", "stock"), is_short=is_short, adv20=self.book.adv20(symbol, day),
        )
        fx = self.book.fx_rate(day) if p["market"] == "US" else 1.0
        proceeds = qty * f.price * fx
        exit_cost = f.cost * fx
        st.cash += (proceeds if not is_short else -proceeds) - exit_cost

        entry_px = float(p["avg_px"])
        entry_notional = qty * entry_px * fx
        gross = qty * (f.price - entry_px) * fx * pos_side
        total_cost = float(p.get("entry_cost_base", 0.0)) + exit_cost + float(p.get("carry_base", 0.0))
        net = gross - total_cost
        entry_date = dt.date.fromisoformat(str(p.get("entry_date", as_of.isoformat())))
        st.positions.pop(symbol, None)

        return {
            "trade_id": f"{account}-{p.get('strategy_id','')}-{symbol}-{entry_date.isoformat()}",
            "account": account, "strategy_id": p.get("strategy_id", ""), "symbol": symbol,
            "market": p["market"], "side": pos_side, "instrument": p.get("instrument", "stock"),
            "family": p.get("family", ""), "signal_date": entry_date.isoformat(),
            "fill_date": entry_date.isoformat(), "exit_date": as_of.isoformat(),
            "fill_px": entry_px, "exit_px": f.price, "qty": qty,
            "cost": float(p.get("entry_cost_base", 0.0)) + exit_cost,
            "borrow_cost": float(p.get("carry_base", 0.0)),
            "pnl": net, "pnl_pct": net / entry_notional if entry_notional else 0.0,
            "closed": 1, "exit_reason": reason,
        }

    def accrue_carry(self, as_of: dt.date) -> float:
        """§7.3 대차수수료·운용보수 일할 누적. 매 거래일 1회만 호출해야 한다."""
        day = PriceBook.to_ord(as_of)
        total = 0.0
        for st in self.state.values():
            for sym, p in st.positions.items():
                px = self.book.last_close(sym, day) or float(p["avg_px"])
                fx = self.book.fx_rate(day) if p["market"] == "US" else 1.0
                carry_local = self.costs.carry(
                    notional=abs(float(p["qty"])) * px,
                    kind=p.get("instrument", "stock"),
                    is_short=float(p["qty"]) < 0,
                )
                carry_base = carry_local * fx
                p["carry_base"] = float(p.get("carry_base", 0.0)) + carry_base
                st.cash -= carry_base
                total += carry_base
        return total

    # ------------------------------------------------------------ 평가

    def nav(self, account: str, as_of: dt.date) -> float:
        day = PriceBook.to_ord(as_of)
        st = self.state.get(account)
        if not st:
            return 0.0
        total = st.cash
        for sym, p in st.positions.items():
            px = self.book.last_close(sym, day) or float(p["avg_px"])
            fx = self.book.fx_rate(day) if p["market"] == "US" else 1.0
            total += float(p["qty"]) * px * fx
        return total

    def exposures(self, account: str, as_of: dt.date) -> dict[str, float]:
        day = PriceBook.to_ord(as_of)
        nav = self.nav(account, as_of) or 1.0
        st = self.state.get(account)
        gross = short = levinv = 0.0
        if st:
            for sym, p in st.positions.items():
                px = self.book.last_close(sym, day) or float(p["avg_px"])
                fx = self.book.fx_rate(day) if p["market"] == "US" else 1.0
                base = abs(float(p["qty"])) * px * fx * float(p.get("leverage", 1.0))
                gross += base
                if float(p["qty"]) < 0:
                    short += base
                if p.get("instrument") in ("lev_etf", "inv_etf"):
                    levinv += base
        return {
            "gross_notional_pct": gross / nav,
            "short_notional_pct": short / nav,
            "lev_inv_notional_pct": levinv / nav,
        }
