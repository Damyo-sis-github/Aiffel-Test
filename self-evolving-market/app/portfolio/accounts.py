"""§8.1 두 계좌 병행.

ACC_S(100만) 는 "이 신호가 내 돈으로 실제 실행 가능한가"를 본다.
ACC_L(1억) 은 통계·공식 판정용. 두 계좌 차이 = 자본 크기 효과 (#24).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import risk as load_risk


@dataclass(frozen=True)
class Account:
    name: str
    capital_krw: float
    position_limit_pct: float
    sector_limit_pct: float
    max_positions: int | None
    min_positions: int | None
    min_shares: float
    official_judgement: bool
    purpose: str = ""


def accounts() -> dict[str, Account]:
    out = {}
    for name, cfg in (load_risk().get("accounts") or {}).items():
        out[name] = Account(
            name=name,
            capital_krw=float(cfg["capital_krw"]),
            position_limit_pct=float(cfg["position_limit_pct"]),
            sector_limit_pct=float(cfg["sector_limit_pct"]),
            max_positions=(int(cfg["max_positions"]) if cfg.get("max_positions") else None),
            min_positions=(int(cfg["min_positions"]) if cfg.get("min_positions") else None),
            min_shares=float(cfg.get("min_shares", 1)),
            official_judgement=bool(cfg.get("official_judgement", False)),
            purpose=str(cfg.get("purpose", "")),
        )
    return out


def official_account() -> str:
    """§1.2 공식 판정 계좌. ACC_S 는 정수 주식 제약으로 왜곡된다."""
    for name, acc in accounts().items():
        if acc.official_judgement:
            return name
    return "ACC_L"
