"""§7.3 비용 모델. **필수 인자, 기본값 없음** (#3 비용 무시 방지).

CostModel 은 config/costs.yaml 없이는 만들어지지 않는다. 비용 0 시나리오는
`CostModel.zero()` 로 명시적으로만 만들 수 있고, 리포트가 두 값을 병기한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import costs as load_costs

LEVERAGED_KINDS = ("lev_etf", "inv_etf")


@dataclass(frozen=True)
class Fill:
    price: float
    cost: float
    slippage_rate: float


@dataclass(frozen=True)
class CostModel:
    markets: dict
    instruments: dict
    short: dict
    liquidity: dict
    delisting: dict
    trading_days_per_year: int
    enabled: bool = True

    # ------------------------------------------------------------ 생성

    @classmethod
    def from_config(cls) -> CostModel:
        c = load_costs()
        return cls(
            markets=c["markets"],
            instruments=c["instruments"],
            short=c["short"],
            liquidity=c["liquidity"],
            delisting=c["delisting"],
            trading_days_per_year=int(c.get("trading_days_per_year", 252)),
        )

    @classmethod
    def zero(cls) -> CostModel:
        """#3 비교용 '비용 0' 모델. 이 모델로 만든 결과는 절대 게이트 판정에 쓰지 않는다."""
        base = cls.from_config()
        return cls(
            markets=base.markets,
            instruments=base.instruments,
            short=base.short,
            liquidity=base.liquidity,
            delisting=base.delisting,
            trading_days_per_year=base.trading_days_per_year,
            enabled=False,
        )

    # ------------------------------------------------------------ 조회

    def slippage_rate(self, market: str, kind: str) -> float:
        if not self.enabled:
            return 0.0
        inst = self.instruments.get(kind, {}) or {}
        if (ov := inst.get("slippage_rate_override")) is not None:
            return float(ov)
        return float(self.markets[market]["slippage_rate"])

    def commission_rate(self, market: str) -> float:
        return 0.0 if not self.enabled else float(self.markets[market].get("commission_rate", 0.0))

    def sell_side_rate(self, market: str) -> float:
        """매도 시에만 붙는 세금·수수료 (KR 거래세, US SEC fee)."""
        if not self.enabled:
            return 0.0
        m = self.markets[market]
        return float(m.get("sell_tax_rate", 0.0)) + float(m.get("sec_fee_rate", 0.0))

    def daily_expense_rate(self, kind: str) -> float:
        """레버리지·인버스 ETF 운용보수 일할 (§7.3)."""
        if not self.enabled:
            return 0.0
        annual = float((self.instruments.get(kind, {}) or {}).get("extra_expense_ratio", 0.0))
        return annual / self.trading_days_per_year

    def daily_borrow_rate(self) -> float:
        """US 공매도 대차수수료 일할."""
        if not self.enabled:
            return 0.0
        return float(self.short.get("borrow_fee_annual", 0.0)) / self.trading_days_per_year

    def hard_to_borrow(self) -> frozenset[str]:
        return frozenset(str(s) for s in (self.short.get("hard_to_borrow_symbols") or []))

    def fractional(self, market: str) -> tuple[bool, float]:
        m = self.markets[market]
        return bool(m.get("fractional_shares", False)), float(m.get("min_fraction", 1.0))

    # ------------------------------------------------------------ 체결가·비용

    def _liquidity_multiplier(self, notional: float, adv20: float | None) -> float:
        """주문 > adv20 × 1% → 슬리피지 ×2 (§7.3). ACC_L 에서만 실제로 걸린다."""
        if not self.enabled or not adv20 or adv20 <= 0:
            return 1.0
        thr = float(self.liquidity.get("adv20_participation_threshold", 0.01))
        if notional > adv20 * thr:
            return float(self.liquidity.get("slippage_multiplier", 2.0))
        return 1.0

    def fill(
        self,
        *,
        ref_price: float,
        qty: float,
        side: int,          # +1 매수/숏커버, -1 매도/숏진입
        opening: bool,      # 신규 진입인가
        market: str,
        kind: str,
        is_short: bool,
        adv20: float | None = None,
    ) -> Fill:
        """체결가(슬리피지 반영) + 거래비용(수수료·세금)."""
        notional = abs(qty) * ref_price
        slip = self.slippage_rate(market, kind) * self._liquidity_multiplier(notional, adv20)
        if is_short and opening:
            slip *= float(self.short.get("entry_slippage_multiplier", 1.0))
        # 항상 불리한 방향으로: 사면 비싸게, 팔면 싸게.
        price = ref_price * (1 + slip) if side > 0 else ref_price * (1 - slip)
        traded = abs(qty) * price
        cost = traded * self.commission_rate(market)
        if side < 0:                      # 매도 계열
            cost += traded * self.sell_side_rate(market)
        return Fill(price=price, cost=cost, slippage_rate=slip)

    def carry(self, *, notional: float, kind: str, is_short: bool, days: int = 1) -> float:
        """보유 비용: 대차수수료 + 레버리지 ETF 운용보수."""
        rate = self.daily_expense_rate(kind)
        if is_short:
            rate += self.daily_borrow_rate()
        return abs(notional) * rate * days
