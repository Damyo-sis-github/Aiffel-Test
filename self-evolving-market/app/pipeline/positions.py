"""페이퍼 포지션 관리 — 백테스트 엔진과 **같은** 하드 제약을 적용한다.

여기가 없으면 포지션이 열리기만 하고 청산되지 않아 W_trade 가 영원히 계산되지 않는다.
(실제로 한 번 그렇게 만들었고, 리포트가 '청산 거래 없음'만 찍었다.)

적용 순서 (§7.3, §7.6)
  1. 상폐 강제 청산 — 마지막가 50% (숏은 마지막가)
  2. 장중 손절 — #20 SHORT -8% 는 무시 불가. 갭이 손절가를 넘으면 시가 체결(낙관 금지)
  3. 보유기간 만료 — 호라이즌, 그리고 #19 LEV/INV 5일 강제
"""

from __future__ import annotations

import datetime as dt

from app.backtest.costs import CostModel
from app.backtest.engine import PriceBook
from app.config import risk as load_risk
from app.execution.paper_sim import PaperBroker
from app.universe.snapshot import symbol_meta


def stop_pct_for(family: str, horizon: int, cfg: dict | None = None) -> float:
    stops = (cfg or load_risk())["stop_loss"].get(family, {})
    if "default" in stops:
        return float(stops["default"])
    key = f"h{horizon}"
    if key in stops:
        return float(stops[key])
    keys = sorted(((int(k[1:]), v) for k, v in stops.items() if k.startswith("h")), key=lambda kv: kv[0])
    for h, v in keys:
        if horizon <= h:
            return float(v)
    return float(keys[-1][1]) if keys else 0.15


def max_hold_for(family: str, cfg: dict | None = None) -> int | None:
    v = ((cfg or load_risk())["hard_constraints"].get("max_hold_days") or {}).get(family)
    return int(v) if v is not None else None


def enforce_horizon(family: str, horizon: int, cfg: dict | None = None) -> int:
    cap = ((cfg or load_risk())["hard_constraints"].get("max_horizon_days") or {}).get(family)
    return min(horizon, int(cap)) if cap is not None else horizon


def _hold_days(book: PriceBook, symbol: str, entry: dt.date, today: dt.date) -> int:
    """그 심볼의 거래일 기준 보유일수."""
    a, b = PriceBook.to_ord(entry), PriceBook.to_ord(today)
    n, cur = 0, a
    while cur < b and n < 400:
        nxt = book.next_bar(symbol, cur)
        if nxt is None:
            break
        cur = nxt[0]
        n += 1
    return n


def manage(broker: PaperBroker, date: dt.date, *, costs: CostModel | None = None) -> list[dict]:
    """오늘 청산해야 할 포지션을 전부 청산하고 실현 거래 목록을 돌려준다."""
    costs = costs or CostModel.from_config()
    cfg = load_risk()
    delist_cfg = costs.delisting
    meta = symbol_meta()
    day = PriceBook.to_ord(date)
    closed: list[dict] = []

    for account in sorted(broker.state):
        st = broker.state[account]
        for symbol in sorted(st.positions):
            p = st.positions.get(symbol)
            if p is None:
                continue
            bar = broker.book.bar(symbol, day)
            if bar is None:
                continue                      # 그 시장의 휴장일 — 건드리지 않는다
            qty = float(p["qty"])
            is_short = qty < 0
            entry = dt.date.fromisoformat(str(p.get("entry_date", date.isoformat())))
            if entry >= date:
                continue                      # 진입 당일은 손절·만기 대상이 아니다

            # 1) 상폐
            m = meta.get(symbol)
            if m and m.delist_date and m.delist_date <= date:
                last = broker.book.last_close(symbol, day) or bar["c"]
                ratio = float(delist_cfg.get("forced_exit_price_ratio", 0.5))
                px = last if is_short else last * ratio
                if (t := broker.close(account, symbol, px, date, "delist")) is not None:
                    closed.append(t)
                continue

            # 2) 장중 손절 (#20)
            stop_px = float(p.get("stop_px", 0.0) or 0.0)
            if stop_px > 0:
                hit = (bar["l"] <= stop_px) if not is_short else (bar["h"] >= stop_px)
                if hit:
                    px = min(bar["o"], stop_px) if not is_short else max(bar["o"], stop_px)
                    if (t := broker.close(account, symbol, px, date, "stop_loss")) is not None:
                        closed.append(t)
                    continue

            # 3) 보유기간 (#19)
            family = str(p.get("family", "LONG"))
            horizon = enforce_horizon(family, int(p.get("horizon_days", 5)), cfg)
            hard = p.get("max_hold_days")
            hard = int(hard) if hard is not None else max_hold_for(family, cfg)
            due = horizon if hard is None else min(horizon, hard)
            held = _hold_days(broker.book, symbol, entry, date)
            # 페이퍼는 당일 시가에 즉시 청산하므로 due 칸을 채운 날 바로 나간다.
            if held >= due:
                reason = "max_hold" if (hard is not None and due == hard and hard < horizon) else "horizon"
                if (t := broker.close(account, symbol, bar["o"], date, reason)) is not None:
                    closed.append(t)
    broker.save()
    return closed
