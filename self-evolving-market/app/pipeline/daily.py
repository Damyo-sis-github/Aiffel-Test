"""§11.4 daily 순서.

1 인제스트 → 게이트
2 전일 체결 반영, 청산 → W_trade, E 갱신
3 예측 리스트 채점 → W_pred
4 피처·레짐·테마·국가·종목
5 신호 → 두 계좌 포트 → 리스크 → 페이퍼 주문 (t+1)
6 예측 리스트 발행
7 트리거 평가 → trigger_pending.json (실행은 19:00 evolve 가)
8 리포트 + 알림 + git push

`daily` 는 순수 Python 이다. **LLM 호출이 없다.**
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from app.alerts.router import AlertRouter, Level
from app.backtest.costs import CostModel
from app.backtest.engine import PriceBook
from app.config import gates as gates_cfg
from app.config import require_protected_lock, runtime
from app.config import risk as load_risk
from app.data.adapters.registry import synthetic_in_use
from app.data.ingest import Ingestor, integrity_rows
from app.data.meta_db import MetaDB
from app.data.pit_store import PITStore
from app.evaluator.family_gates import family_activation_open
from app.evaluator.stats import alpha_for_k, wilson_ci
from app.evolve.context_pack import build_context_pack, diagnose, write_context_pack
from app.evolve.curator import Curator
from app.evolve.trigger import evaluate_triggers, write_pending
from app.execution.broker import Order as BrokerOrder
from app.execution.divergence import assumed_fill_price
from app.execution.divergence import check as divergence_check
from app.execution.paper_sim import PaperBroker
from app.features.builder import FeatureBuilder
from app.features.regime import regime_matrix
from app.guards import DAILY_GUARDS, GuardViolation, run_guards
from app.guards.base import bypass_enabled
from app.paths import state_dir
from app.pipeline import state as snap_state
from app.pipeline.positions import enforce_horizon, max_hold_for, stop_pct_for
from app.pipeline.positions import manage as manage_positions
from app.portfolio.accounts import accounts
from app.portfolio.killswitch import KillSwitch
from app.portfolio.risk import Order as RiskOrder
from app.portfolio.risk import RiskEngine
from app.predictions.publish import publish_predictions
from app.predictions.score import rolling_edge, score_predictions, summarize
from app.reports.daily import DailyReport, render_daily, write_daily
from app.reports.dashboard import refresh_quietly
from app.strategies.registry import all_strategies, get_strategy
from app.universe.snapshot import UniverseBuilder, all_symbols, kind_of
from app.util.calendars import CalendarCoverageError, is_trading_day, trading_days
from app.util.hashing import hash_obj

PENDING_ORDERS_FILE = "pending_orders.json"
PREDICTIONS_FILE = "predictions.json"
RANKINGS_FILE = "rankings.json"
FEATURE_LOOKBACK_DAYS = 400


@dataclass
class DailyOutcome:
    date: dt.date
    ok: bool
    mode: str
    result_hash: str
    report_path: Path | None = None
    summary: str = ""
    halted: bool = False
    messages: list[str] = field(default_factory=list)

    def line(self) -> str:
        mark = "OK" if self.ok else ("중단" if self.halted else "실패")
        return f"[{self.date}] daily {mark} ({self.mode}) hash={self.result_hash}"


class DailyRunner:
    def __init__(
        self,
        *,
        store: PITStore | None = None,
        db: MetaDB | None = None,
        alerts: AlertRouter | None = None,
        skip_guards: bool = False,
    ):
        self.store = store or PITStore()
        self.db = db or MetaDB()
        self.alerts = alerts or AlertRouter(self.db)
        self.skip_guards = skip_guards
        self.risk_cfg = load_risk()
        self.killswitch = KillSwitch()
        self.curator = Curator(self.db)

    # ------------------------------------------------------------ 가드

    def check_guards(self) -> list[str]:
        if self.skip_guards:
            return ["가드 검사 생략 (--skip-guards, 개발 전용)"]
        return [str(r) for r in run_guards(DAILY_GUARDS, raise_on_fail=True)]

    # ------------------------------------------------------------ 실행

    def run(
        self,
        date: dt.date,
        *,
        market: str | None = None,
        mode: str = "실시간",
        push: bool = False,
        refresh_dashboard: bool = True,
    ) -> DailyOutcome:
        msgs: list[str] = []
        try:
            msgs.extend(self.check_guards())
        except GuardViolation as exc:
            self.alerts.send(Level.CRITICAL, f"daily 실행 거부 ({date})\n{exc}")
            return DailyOutcome(date, False, mode, "", summary=str(exc), messages=[str(exc)])

        # protected.lock 은 가드에서도 보지만, 명시적으로 한 번 더 (#6)
        try:
            require_protected_lock()
        except Exception as exc:
            if not self.skip_guards:
                self.alerts.critical(f"protected.lock 불일치로 daily 중단 ({date}): {exc}")
                return DailyOutcome(date, False, mode, "", summary=str(exc), messages=[str(exc)])
            msgs.append(f"protected.lock 경고(개발 모드): {exc}")

        rerun = snap_state.prepare(date)
        if rerun:
            msgs.append("동일 날짜 재실행 — 스냅샷 복원 후 처리 (멱등성 #11)")

        # ---- 1. 인제스트 + 무결성 게이트
        ing = Ingestor(self.store)
        markets = [market.upper()] if market else ["KR", "US"]
        integrity_summary, quarantined, halted = [], set(), False
        for mk in markets:
            res = ing.ingest_prices(mk, date, date)
            self.db.upsert("integrity_events", integrity_rows(res.report))
            integrity_summary.append(f"{mk}: {res.report.summary()}")
            quarantined |= set(res.report.quarantined)
            halted |= res.halted
        ing.ingest_macro(date, date)
        ing.ingest_fx(date, date)

        if halted:
            msg = f"데이터 게이트 '중단' 등급 ({date}). 재개는 사람 게이트 G3."
            self.alerts.critical(msg)
            return DailyOutcome(date, False, mode, "", summary=msg, halted=True, messages=msgs + [msg])

        # 월말이면 유니버스 스냅샷 (§5.6)
        if self._is_month_end(date):
            UniverseBuilder(self.store).write(date)
            msgs.append("월말 유니버스 스냅샷 기록")

        # ---- 데이터 준비 (PIT)
        start = date - dt.timedelta(days=FEATURE_LOOKBACK_DAYS)
        prices = self.store.prices(date, start=start)
        if prices.empty:
            msg = f"{date} 기준 가격 데이터가 없습니다."
            return DailyOutcome(date, False, mode, "", summary=msg, messages=msgs + [msg])
        book = PriceBook(prices, self.store.fx(date, pair="USDKRW"))
        panel = FeatureBuilder(self.store).build(date, start=start)
        costs = CostModel.from_config()
        accs = accounts()
        broker = PaperBroker(book, costs, {n: a.capital_krw for n, a in accs.items()})

        # ---- 2. 전일 신호 체결 (t+1 시가) → 하드 제약 청산 → W_trade, E
        fills = self._execute_pending(broker, date)
        broker.accrue_carry(date)
        realized = manage_positions(broker, date, costs=costs)
        if realized:
            self.db.upsert("trades", realized)
        trades_today = [
            {"account": f.order.account, "symbol": f.order.symbol, "side": f.order.side,
             "qty": f.qty, "price": f.price, "reason": f.order.reason}
            for f in fills if not f.rejected
        ] + [
            {"account": t["account"], "symbol": t["symbol"], "side": -int(t["side"]),
             "qty": t["qty"], "price": t["exit_px"], "reason": f"청산/{t['exit_reason']}"}
            for t in realized
        ]
        for f in fills:
            if f.rejected:
                msgs.append(f"체결 거부 {f.order.symbol}: {f.reject_reason}")
        self._record_trades(fills, date, book=book, costs=costs)

        # ---- 3. 예측 채점 → W_pred
        preds = self._load_predictions()
        preds = score_predictions(preds, book, date) if not preds.empty else preds
        universe = [s for s in all_symbols() if kind_of(s) == "stock"][:40]
        pred_stats = summarize(preds, book, universe) if not preds.empty else []
        edge_ma = float(rolling_edge(preds).iloc[-1]) if not preds.empty and len(rolling_edge(preds)) else None

        # ---- 4·5. 피처 → 신호 → 두 계좌 리스크 → 페이퍼 주문 (t+1)
        snap = panel.on(date)
        active = self._active_strategies()
        new_orders: list[dict] = []
        risk_notes: list[str] = []
        if not snap.empty and active:
            for acc_name in sorted(accs):
                orders, notes = self._build_orders(active, snap, date, acc_name, broker)
                new_orders.extend(orders)
                risk_notes.extend(notes)
        self._save_pending_orders(new_orders)

        # ---- 6. 예측 리스트 발행
        if not snap.empty and active:
            fresh = publish_predictions(snap, date, [(s, w) for s, w, _ in active])
            if not fresh.empty:
                preds = pd.concat([preds, fresh], ignore_index=True) if not preds.empty else fresh
                preds = preds.drop_duplicates(subset=["pred_date", "horizon", "side", "rank"], keep="last")
        self._save_predictions(preds)
        if not preds.empty:
            self.db.upsert("predictions", preds.to_dict("records"))

        # ---- NAV·킬스위치
        acc_report: dict[str, dict] = {}
        ks_state = self.killswitch.read()
        for name, acc in accs.items():
            nav = broker.nav(name, date)
            exp = broker.exposures(name, date)
            dd = self._drawdown(name, nav, date)
            acc_report[name] = {"nav": nav, "cash": broker.cash(name).amount, "capital": acc.capital_krw,
                                "drawdown": dd, **exp}
            self.db.upsert("nav", [{"date": date.isoformat(), "account": name, "nav": nav,
                                    "cash": broker.cash(name).amount, **exp, "drawdown": dd,
                                    "source": mode}])
            if acc.official_judgement:
                ks_state = self.killswitch.evaluate(abs(min(dd, 0.0)), name, date)
        if ks_state.tripped:
            self.alerts.critical(ks_state.message())

        # ---- 7. 트리거 평가
        all_trades = self.db.query("SELECT * FROM trades")
        triggers = []
        # ---- 7b. §9 괴리 추적 → quarantine
        # 백테스트 가정 체결가와 페이퍼 체결가가 20거래 평균 0.3% 넘게 갈라지면
        # 그 전략의 페이퍼 성과를 더 이상 믿을 수 없다. 배분을 0 으로 내린다.
        div_gaps, div_bad, div_summary = divergence_check(all_trades)
        for sid in div_bad:
            gap = float(div_gaps[sid])
            reason = f"§9 괴리 {gap * 100:.3f}% > 0.300% (최근 20거래 평균)"
            tr = self.curator.quarantine(sid, reason)
            msgs.append(f"괴리 격리: {sid} — {reason}")
            self.alerts.critical(f"[{date}] 괴리 격리 {sid}\n{reason}\n"
                                 "백테스트와 페이퍼가 갈라졌습니다. 상태 드리프트를 의심하십시오.")
            self.db.log_evolution(ts=dt.datetime.now().isoformat(timespec="seconds"),
                                  action="state_transition", before=tr.before, after=tr.after,
                                  reason=reason)

        if not ks_state.tripped:
            triggers = evaluate_triggers(
                today=date,
                trades=all_trades,
                regime_row=panel.regime_on(date),
                last_cycles=self._last_cycles(),
                pred_edge_ma=edge_ma,
                is_first_trading_day_of_month=self._is_first_trading_day(date),
            )
            write_pending(triggers, dt.datetime.combine(date, dt.time(16, 30)))
            # 트리거가 있으면 컨텍스트 팩도 **같이** 쓴다.
            # `/evolve` 0단계가 state/context_pack.json 을 읽는데 그걸 만드는 코드가
            # 아무 데서도 불리지 않았다. 그 상태로 진화를 돌리면 LLM 이 성과·레짐·
            # 랭킹·누적 K·계열 게이트·실패 진단을 하나도 못 본 채 가설을 만든다.
            if any(t.is_evolution for t in triggers) and (
                cp_msg := self._save_context_pack(date, triggers, all_trades, panel, pred_stats)
            ):
                msgs.append(cp_msg)
        else:
            msgs.append("킬스위치 발동 상태 — 신호·진화 정지 (§8.2)")

        # ---- 8. 리포트 + 알림
        rep = DailyReport(
            date=date, mode=mode, accounts=acc_report, trades_today=trades_today,
            performance=self._performance(all_trades), prediction_stats=pred_stats,
            exposures_by_family=self._family_exposure(broker, date),
            strategy_states=self.curator.states().to_dict("records"),
            integrity={"summary": " / ".join(integrity_summary), "quarantined": sorted(quarantined)},
            divergence={"summary": div_summary, "quarantined": div_bad,
                        "by_strategy": {k: (None if v != v else round(float(v), 6))
                                        for k, v in div_gaps.items()}},
            killswitch=ks_state.message(),
            triggers=[t.__dict__ for t in triggers],
            synthetic=synthetic_in_use(), bypassed=bypass_enabled(),
            has_short=any(o["side"] < 0 for o in new_orders),
            notes=risk_notes[:10] + msgs[:10],
        )
        body, telegram = render_daily(rep)
        path = write_daily(rep, body)
        self.alerts.info(telegram)
        self.alerts.retry_failed()

        result_hash = hash_obj(
            {
                "date": date.isoformat(),
                "accounts": {k: {kk: round(float(vv), 6) for kk, vv in v.items() if isinstance(vv, (int, float))}
                             for k, v in acc_report.items()},
                "orders": sorted((o["symbol"], o["account"], o["side"], round(float(o["qty"]), 6))
                                 for o in new_orders),
                "fills": sorted((f.order.symbol, f.order.account, round(f.price, 6), round(f.qty, 6))
                                for f in fills if not f.rejected),
                "triggers": sorted(t.code + "|" + t.target for t in triggers),
                "quarantined": sorted(quarantined),
                "divergence_quarantined": div_bad,
            }
        )
        self.db.upsert("run_log", [{
            "run_date": date.isoformat(), "task": "daily", "mode": mode,
            "started_at": dt.datetime.now().isoformat(timespec="seconds"),
            "finished_at": dt.datetime.now().isoformat(timespec="seconds"),
            "status": "ok", "result_hash": result_hash,
            "catchup": int(mode == "보충"), "note": "; ".join(msgs[:5]),
        }])
        # 3계층 랭킹은 **여기서** 계산해 저장한다. 이미 만들어 둔 패널을 그대로 쓴다.
        # 대시보드가 자기가 다시 만들면 400일치 피처 패널을 한 벌 더 짓게 되고,
        # daily 가 그만큼 느려진다. 리포트는 계산하는 곳이 아니라 읽는 곳이다.
        if (rank_msg := self._save_rankings(panel, date)):
            msgs.append(rank_msg)

        # 대시보드를 여기서 다시 만든다. 열어둔 페이지가 이걸 읽어간다.
        # result_hash 계산이 끝난 **뒤**라서 멱등성·replay 검증에 끼어들지 않는다.
        # 보충(catchup) 루프에서는 끄고, 마지막에 한 번만 만든다 —
        # 랭킹 재계산이 무거워서 60일치를 매일 다시 그리면 몇 분이 그냥 날아간다.
        if refresh_dashboard:
            refresh_quietly(date)

        if push:
            self._git_push(date)
        return DailyOutcome(date, True, mode, result_hash, path, telegram, messages=msgs)

    # ------------------------------------------------------------ catchup

    def catchup(self, until: dt.date, *, push: bool = False) -> list[DailyOutcome]:
        """§11.1 놓친 날 자동 보충. 날짜별 PIT 쿼리를 그대로 쓰므로 결과가 실시간 실행과 같다 (#32)."""
        last = self.db.scalar("SELECT MAX(run_date) FROM run_log WHERE task = 'daily' AND status = 'ok'")
        cfg = runtime().get("catchup", {})
        max_days = int(cfg.get("max_days", 60))
        if last:
            start = dt.date.fromisoformat(str(last)) + dt.timedelta(days=1)
        else:
            start = until - dt.timedelta(days=5)
        try:
            days = sorted(set(trading_days("KR", start, until)) | set(trading_days("US", start, until)))
        except CalendarCoverageError:
            days = [d for d in pd.date_range(start, until).date if d.weekday() < 5]
        if len(days) > max_days:
            msg = f"보충할 날이 {len(days)}일로 상한 {max_days}일을 넘습니다. 사람 확인이 필요합니다."
            self.alerts.critical(msg)
            return [DailyOutcome(until, False, "보충", "", summary=msg)]
        out = []
        for d in days:
            out.append(self.run(d, mode="보충", push=False, refresh_dashboard=False))
            if not out[-1].ok:
                break
        if out and out[-1].ok:
            refresh_quietly(out[-1].date)
        if push and out and out[-1].ok:
            self._git_push(until)
        return out

    # ------------------------------------------------------------ 내부

    def _active_strategies(self) -> list[tuple]:
        """(전략, 배분비중, 상태). active 가 없으면 시드를 candidate 로 간주해 페이퍼를 돌린다."""
        states = self.curator.states()
        registry = {s.id: s for s in all_strategies()}
        if not states.empty:
            act = states[(states["status"].isin(["active", "candidate"])) & (states["quarantined"] == 0)]
            if not act.empty:
                out = []
                for _, r in act.iterrows():
                    sid = str(r["strategy_id"])
                    if sid in registry:
                        w = float(r["allocation_pct"]) or 1.0 / len(act)
                        out.append((registry[sid], w, str(r["status"])))
                return out
        # Phase 2 초기: 상태 테이블이 비어 있으면 벤치마크·대조군을 뺀 시드로 페이퍼를 돌린다.
        seeds = [s for s in registry.values() if not s.id.startswith(("bench_", "random_ctrl"))]
        return [(s, 1.0 / len(seeds), "candidate") for s in seeds] if seeds else []

    def _build_orders(self, active, snap, date, account, broker) -> tuple[list[dict], list[str]]:
        engine = RiskEngine(account, self.risk_cfg)
        acc = accounts()[account]
        nav = broker.nav(account, date) or acc.capital_krw
        current = {
            p.symbol: (p.qty * (broker.book.last_close(p.symbol, PriceBook.to_ord(date)) or p.avg_px)
                       * (broker.book.fx_rate(PriceBook.to_ord(date)) if p.market == "US" else 1.0)) / nav
            for p in broker.positions(account)
        }
        raw: list[RiskOrder] = []
        spec: dict[str, tuple] = {}     # symbol → (family, horizon, stop_pct, max_hold)
        for strat, weight, _status in active:
            sig = strat.signals(snap, date)
            if sig.empty:
                continue
            family = str(strat.family)
            horizon = enforce_horizon(family, int(strat.horizon_days), self.risk_cfg)
            stop = stop_pct_for(family, horizon, self.risk_cfg)
            hard = max_hold_for(family, self.risk_cfg)
            per = weight / max(len(sig), 1)
            for _, r in sig.iterrows():
                sym = str(r["symbol"])
                if sym in current:
                    continue
                raw.append(RiskOrder(sym, int(r["side"]), per, strat.id, family))
                spec.setdefault(sym, (family, horizon, stop, hard))
        if not raw:
            return [], []

        checked = engine.check(raw, current)
        notes = [f"[{account}] {n}" for n in checked.reasons()[:5]]
        day = PriceBook.to_ord(date)
        fx = broker.book.fx_rate(day)
        orders = []
        for o in checked.accepted:
            px = broker.book.last_close(o.symbol, day)
            if not px or px <= 0:
                continue
            lev = max(o.leverage, 1.0)
            base = nav * o.weight / lev
            local = base / (fx if o.market == "US" else 1.0)
            qty = local / px
            frac, min_frac = CostModel.from_config().fractional(o.market)
            qty = np.floor(qty / min_frac) * min_frac if frac else float(np.floor(qty))
            if qty < (acc.min_shares if not frac else min_frac):
                notes.append(f"[{account}] {o.symbol}: 최소 수량 미달로 폐기 (정수 주식 제약)")
                continue
            family, horizon, stop, hard = spec.get(o.symbol, (o.family, 5, 0.15, None))
            orders.append({
                "symbol": o.symbol, "market": o.market, "side": o.side, "qty": float(qty),
                "strategy_id": o.strategy_id, "account": account,
                "signal_date": date.isoformat(), "reason": "signal", "instrument": kind_of(o.symbol),
                "family": family, "horizon_days": int(horizon),
                "stop_pct": float(stop), "max_hold_days": hard,
            })
        return orders, notes

    def _execute_pending(self, broker: PaperBroker, date: dt.date):
        p = state_dir() / PENDING_ORDERS_FILE
        if not p.exists():
            return []
        rows = json.loads(p.read_text(encoding="utf-8"))
        orders = [
            BrokerOrder(
                symbol=r["symbol"], market=r["market"], side=int(r["side"]), qty=float(r["qty"]),
                strategy_id=r["strategy_id"], account=r["account"],
                signal_date=dt.date.fromisoformat(r["signal_date"]), reason=r.get("reason", "signal"),
                instrument=r.get("instrument", "stock"),
                family=r.get("family", "LONG"), horizon_days=int(r.get("horizon_days", 5)),
                stop_pct=float(r.get("stop_pct", 0.0)),
                max_hold_days=r.get("max_hold_days"),
            )
            for r in rows
        ]
        return broker.submit(orders, date)

    def _save_pending_orders(self, orders: list[dict]) -> None:
        p = state_dir() / PENDING_ORDERS_FILE
        p.write_text(json.dumps(sorted(orders, key=lambda o: (o["account"], o["symbol"])),
                                ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    def _load_predictions(self) -> pd.DataFrame:
        """날짜는 **문자열로** 유지한다. Timestamp 로 바뀌면 SQLite 바인딩이 깨진다."""
        p = state_dir() / PREDICTIONS_FILE
        if not p.exists() or p.stat().st_size <= 2:
            return pd.DataFrame()
        rows = json.loads(p.read_text(encoding="utf-8"))
        return _normalize_predictions(pd.DataFrame(rows))

    def _save_predictions(self, preds: pd.DataFrame) -> None:
        p = state_dir() / PREDICTIONS_FILE
        if preds is None or preds.empty:
            p.write_text("[]", encoding="utf-8")
            return
        # 오래된 예측은 채점이 끝나면 잘라낸다 (파일 비대 방지).
        keep = _normalize_predictions(preds).sort_values("pred_date").tail(20000)
        p.write_text(json.dumps(keep.to_dict("records"), ensure_ascii=False), encoding="utf-8")

    def _save_context_pack(self, date, triggers, all_trades, panel, pred_stats) -> str | None:
        """§10.3 진화 입력. 실패해도 daily 를 죽이지 않는다 — 트리거는 이미 기록됐다."""
        try:
            closed = all_trades[all_trades["closed"] == 1] if not all_trades.empty else all_trades
            states = self.curator.states()
            active_by_family: dict[str, int] = {}
            if not states.empty:
                act = states[(states["status"] == "active") & (states["quarantined"] == 0)]
                active_by_family = {str(k): int(v) for k, v in act["family"].value_counts().items()}
            open_families = {
                f: family_activation_open(f, active_by_family)[0]
                for f in sorted(set(states["family"]) if not states.empty else set())
                if f
            }
            k = self.db.current_k()
            pack = build_context_pack(
                today=date,
                triggers=[t.__dict__ for t in triggers],
                performance=pd.DataFrame(self._performance(all_trades)).T.reset_index(names="strategy_id"),
                regime_matrix=regime_matrix(closed, panel.regime) if not closed.empty else pd.DataFrame(),
                rankings=FeatureBuilder(self.store).rankings(panel, date),
                prediction_stats=pred_stats,
                strategy_states=states,
                k_index=k,
                alpha_k=alpha_for_k(float(gates_cfg().get("control_base_alpha", 0.05)), max(k, 1)),
                diagnoses={
                    str(sid): diagnose(g)
                    for sid, g in (closed.groupby("strategy_id") if not closed.empty else [])
                },
                active_by_family=active_by_family,
                open_families=open_families,
            )
            write_context_pack(pack)
        except Exception as exc:
            return f"컨텍스트 팩 생성 실패: {exc} — /evolve 가 성과 데이터 없이 돌게 됩니다"
        return None

    def _save_rankings(self, panel, date: dt.date) -> str | None:
        """§6.3~6.5 테마 → 국가 → 종목. 대시보드가 읽는다. 실패해도 daily 를 죽이지 않는다."""
        try:
            r = FeatureBuilder(self.store).rankings(panel, date)
            payload = {
                "as_of": date.isoformat(),
                "themes": r["themes"].head(12).to_dict("records") if not r["themes"].empty else [],
                "countries": r["countries"].head(12).to_dict("records") if not r["countries"].empty else [],
                "screener": r["screener"].head(12).to_dict("records") if not r["screener"].empty else [],
            }
            path = state_dir() / RANKINGS_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, default=str, indent=1),
                            encoding="utf-8")
        except Exception as exc:
            return f"랭킹 저장 실패: {exc}"
        return None

    def _record_trades(self, fills, date: dt.date, book: PriceBook | None = None,
                       costs: CostModel | None = None) -> None:
        """§9 괴리 추적을 위해 백테스트 가정 체결가(bt_px)를 함께 남긴다.

        나중에 계산할 수 없다. 그날의 adv20 과 주문 인자가 있어야 하는 값이다.
        """
        day = PriceBook.to_ord(date)
        rows = []
        for f in fills:
            if f.rejected:
                continue
            o = f.order
            bt_px = None
            if book is not None and costs is not None:
                bar = book.bar(o.symbol, day)
                if bar is not None:
                    bt_px = assumed_fill_price(costs, book, o, day, bar["o"])
            rows.append({
                "trade_id": f"{o.account}-{o.strategy_id}-{o.symbol}-{date.isoformat()}",
                "account": o.account, "strategy_id": o.strategy_id, "symbol": o.symbol,
                "market": o.market, "side": o.side, "instrument": o.instrument,
                "family": "", "signal_date": o.signal_date.isoformat(),
                "fill_date": date.isoformat(), "fill_px": f.price, "exit_date": None, "exit_px": None,
                "qty": f.qty, "cost": f.cost, "borrow_cost": 0.0, "pnl": None, "pnl_pct": None,
                "closed": 0, "exit_reason": None, "bt_px": bt_px,
            })
        if rows:
            self.db.upsert("trades", rows)

    def _performance(self, trades: pd.DataFrame) -> dict:
        if trades is None or trades.empty:
            return {}
        closed = trades[trades["closed"] == 1]
        out = {}
        for sid, g in closed.groupby("strategy_id"):
            r = g["pnl_pct"].astype(float).dropna()
            if r.empty:
                continue
            n = len(r)
            out[sid] = {
                "n": n, "win_rate": float((r > 0).mean()), "expectancy": float(r.mean()),
                "ci": wilson_ci(int((r > 0).sum()), n),
            }
        return out

    def _family_exposure(self, broker: PaperBroker, date: dt.date) -> dict:
        day = PriceBook.to_ord(date)
        out: dict[str, float] = {}
        for name in broker.state:
            nav = broker.nav(name, date) or 1.0
            for p in broker.positions(name):
                px = broker.book.last_close(p.symbol, day) or p.avg_px
                fx = broker.book.fx_rate(day) if p.market == "US" else 1.0
                key = f"{name}/{p.instrument}"
                out[key] = out.get(key, 0.0) + abs(p.qty) * px * fx * p.leverage / nav
        return out

    def _drawdown(self, account: str, nav: float, date: dt.date) -> float:
        hist = self.db.query(
            "SELECT nav FROM nav WHERE account = ? AND date <= ? ORDER BY date", (account, date.isoformat())
        )
        peak = max([nav, *(hist["nav"].astype(float).tolist() if not hist.empty else [])])
        return (nav / peak - 1.0) if peak > 0 else 0.0

    def _last_cycles(self) -> dict[str, dict]:
        df = self.db.query(
            "SELECT cycle_id, ts, after FROM evolution_log WHERE action = 'cycle_complete' ORDER BY seq"
        )
        out: dict[str, dict] = {}
        for _, r in df.iterrows():
            try:
                after = json.loads(r["after"]) if r["after"] else {}
            except json.JSONDecodeError:
                continue
            for sid in after.get("targets", []):
                out[sid] = {"date": str(r["ts"])[:10], "cycle_id": r["cycle_id"]}
        return out

    @staticmethod
    def _is_month_end(date: dt.date) -> bool:
        return (date + dt.timedelta(days=1)).month != date.month

    @staticmethod
    def _is_first_trading_day(date: dt.date) -> bool:
        if date.day > 7:
            return False
        d = date.replace(day=1)
        while d < date:
            try:
                if is_trading_day("KR", d) or is_trading_day("US", d):
                    return False
            except CalendarCoverageError:
                if d.weekday() < 5:
                    return False
            d += dt.timedelta(days=1)
        return True

    def _git_push(self, date: dt.date) -> None:
        import subprocess

        from app.paths import repo_root

        try:
            subprocess.run(["git", "add", "-A"], cwd=repo_root(), check=False, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"daily {date.isoformat()}"],
                           cwd=repo_root(), check=False, capture_output=True)
            subprocess.run(["git", "push"], cwd=repo_root(), check=False, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.alerts.send(Level.WARN, f"git push 실패 ({date}): {type(exc).__name__}")


def _normalize_predictions(df: pd.DataFrame) -> pd.DataFrame:
    """SQLite/JSON 왕복에서 타입이 흔들리지 않게 고정한다 (#11 멱등성)."""
    if df is None or df.empty:
        return df
    out = df.copy()
    for col in ("pred_date", "scored_at"):
        if col in out.columns:
            out[col] = out[col].map(lambda v: None if v is None or v != v else str(v)[:10])
    for col in ("horizon", "rank", "side"):
        if col in out.columns:
            out[col] = out[col].astype(int)
    if "hit" in out.columns:
        out["hit"] = out["hit"].map(lambda v: None if v is None or v != v else int(v))
    if "score" in out.columns:
        out["score"] = out["score"].astype(float)
    return out


def strategy_or_none(sid: str):
    try:
        return get_strategy(sid)
    except KeyError:
        return None
