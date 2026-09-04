"""§9 Broker 인터페이스.

이 저장소에는 `PaperBroker` 구현만 존재한다.
`LiveBroker` 는 **별도 저장소·별도 명세·G4 이후**이며 이 파일에도, 이 저장소 어디에도 없다 (§1.3, #16).

포트폴리오·리스크·킬스위치 코드는 Broker 구현과 무관하게 동작해야 한다.
그것이 나중에 실전 모듈을 끼울 수 있는 유일한 경로다.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Money:
    amount: float
    currency: str = "KRW"

    def __add__(self, other: Money) -> Money:
        if self.currency != other.currency:
            raise ValueError(f"통화가 다릅니다: {self.currency} vs {other.currency} (#10 환율 혼합 방지)")
        return Money(self.amount + other.amount, self.currency)


@dataclass(frozen=True)
class Order:
    symbol: str
    market: str
    side: int                 # +1 매수/커버, -1 매도/숏
    qty: float
    strategy_id: str
    account: str
    signal_date: dt.date
    reason: str = "signal"
    instrument: str = "stock"
    # 엔진 하드 제약을 페이퍼에도 동일하게 적용하기 위한 필드 (§7.6, #19·#20)
    family: str = "LONG"
    horizon_days: int = 5
    stop_pct: float = 0.0
    max_hold_days: int | None = None


@dataclass(frozen=True)
class Fill:
    order: Order
    fill_date: dt.date
    price: float
    qty: float
    cost: float
    rejected: bool = False
    reject_reason: str = ""


@dataclass(frozen=True)
class Position:
    account: str
    symbol: str
    market: str
    qty: float
    avg_px: float
    strategy_id: str
    instrument: str = "stock"
    leverage: float = 1.0


@runtime_checkable
class Broker(Protocol):
    def submit(self, orders: list[Order], as_of: dt.date) -> list[Fill]: ...
    def positions(self, account: str) -> list[Position]: ...
    def cash(self, account: str) -> Money: ...
