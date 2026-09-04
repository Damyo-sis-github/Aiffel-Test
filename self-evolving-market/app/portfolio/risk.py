"""§8.2 리스크 체크. **신호 후·주문 전**에 돌고, 초과 주문은 축소/폐기하고 사유를 남긴다."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import risk as load_risk
from app.universe.snapshot import kind_of, leverage_of, market_of, symbol_meta

LEV_KINDS = ("lev_etf", "inv_etf")


@dataclass
class Order:
    symbol: str
    side: int             # +1 매수, -1 숏
    weight: float         # NAV 대비 목표 비중 (명목 아님)
    strategy_id: str = ""
    family: str = ""

    @property
    def market(self) -> str:
        return market_of(self.symbol)

    @property
    def kind(self) -> str:
        return kind_of(self.symbol)

    @property
    def leverage(self) -> float:
        return abs(leverage_of(self.symbol))

    @property
    def notional_weight(self) -> float:
        return self.weight * self.leverage


@dataclass
class RiskCheck:
    accepted: list[Order] = field(default_factory=list)
    reduced: list[tuple[Order, float, str]] = field(default_factory=list)
    rejected: list[tuple[Order, str]] = field(default_factory=list)

    def reasons(self) -> list[str]:
        out = [f"{o.symbol}: 축소 {o.weight:.3f}→{w:.3f} ({why})" for o, w, why in self.reduced]
        out += [f"{o.symbol}: 폐기 ({why})" for o, why in self.rejected]
        return out


class RiskEngine:
    def __init__(self, account: str = "ACC_L", cfg: dict | None = None):
        self.cfg = cfg or load_risk()
        self.account = account
        acc = self.cfg["accounts"][account]
        self.position_limit = float(acc["position_limit_pct"])
        self.sector_limit = float(acc["sector_limit_pct"])
        lim = self.cfg["limits"]
        self.cash_min = float(lim["cash_min_pct"])
        self.gross_max = float(lim["gross_notional_max_pct"])
        self.short_max = float(lim["short_notional_max_pct"])
        self.levinv_max = float(lim["lev_inv_notional_max_pct"])
        self.market_target = lim["market_weight_target"]
        self.market_tol = float(lim["market_weight_tolerance_pp"])
        self.meta = symbol_meta()

    def check(self, orders: list[Order], current: dict[str, float] | None = None) -> RiskCheck:
        """current: 심볼 → 현재 NAV 대비 비중(부호 포함)."""
        out = RiskCheck()
        cur = dict(current or {})
        gross = sum(abs(w) * abs(leverage_of(s)) for s, w in cur.items())
        short = sum(abs(w) * abs(leverage_of(s)) for s, w in cur.items() if w < 0)
        levinv = sum(abs(w) * abs(leverage_of(s)) for s, w in cur.items() if kind_of(s) in LEV_KINDS)
        sector: dict[str, float] = {}
        for s, w in cur.items():
            key = self._sector_key(s)
            sector[key] = sector.get(key, 0.0) + abs(w)
        invested = sum(abs(w) for w in cur.values())

        # 결정론: 비중 큰 주문부터 처리한다.
        for o in sorted(orders, key=lambda x: (-abs(x.weight), x.symbol)):
            w = abs(o.weight)
            why = []

            if w > self.position_limit:
                why.append(f"종목 한도 {self.position_limit:.0%}")
                w = self.position_limit

            sec = self._sector_key(o.symbol)
            room_sector = self.sector_limit - sector.get(sec, 0.0)
            if w > room_sector:
                why.append(f"섹터 한도 {self.sector_limit:.0%}")
                w = max(0.0, room_sector)

            room_cash = (1.0 - self.cash_min) - invested
            if w > room_cash:
                why.append(f"현금 하한 {self.cash_min:.0%}")
                w = max(0.0, room_cash)

            nw = w * o.leverage
            if gross + nw > self.gross_max:
                why.append(f"명목 익스포저 {self.gross_max:.0%}")
                nw = max(0.0, self.gross_max - gross)
                w = nw / o.leverage if o.leverage else 0.0
            if o.side < 0 and short + nw > self.short_max:
                why.append(f"숏 명목 {self.short_max:.0%}")
                nw = max(0.0, self.short_max - short)
                w = nw / o.leverage if o.leverage else 0.0
            if o.kind in LEV_KINDS and levinv + nw > self.levinv_max:
                why.append(f"레버리지+인버스 명목 {self.levinv_max:.0%}")
                nw = max(0.0, self.levinv_max - levinv)
                w = nw / o.leverage if o.leverage else 0.0

            if w <= 1e-6:
                out.rejected.append((o, ", ".join(why) or "비중 0"))
                continue
            if why:
                out.reduced.append((o, w, ", ".join(why)))
            # weight 는 항상 크기(양수), 방향은 side 가 갖는다.
            out.accepted.append(Order(o.symbol, o.side, w, o.strategy_id, o.family))

            gross += w * o.leverage
            if o.side < 0:
                short += w * o.leverage
            if o.kind in LEV_KINDS:
                levinv += w * o.leverage
            sector[sec] = sector.get(sec, 0.0) + w
            invested += w
        return out

    def market_balance_ok(self, weights_by_market: dict[str, float]) -> tuple[bool, str]:
        """§8.2 KR/US 50/50 ± 20%p."""
        total = sum(abs(v) for v in weights_by_market.values())
        if total <= 0:
            return True, "포지션 없음"
        for market, target in self.market_target.items():
            actual = abs(weights_by_market.get(market, 0.0)) / total
            if abs(actual - float(target)) > self.market_tol:
                return False, f"{market} 비중 {actual:.0%} 가 목표 {float(target):.0%} ± {self.market_tol:.0%} 밖"
        return True, "시장 비중 정상"

    def _sector_key(self, symbol: str) -> str:
        m = self.meta.get(symbol)
        if not m:
            return "UNKNOWN"
        return m.sector or m.theme or m.kind
