"""CLI — daily weekly monthly replay resume lock healthcheck evolve backfill evaluate calendar.

사람 게이트 (자동화 금지, §3.6)
  G1 `resume --ack`            킬스위치 해제
  G2 `lock --update --ack`     evaluator/·gates·risk 변경 승인
  G3 데이터 게이트 '중단' 후 재개 (`resume --data --ack`)
  G4 실전 전환 — 이 저장소엔 실전 주문 코드가 존재하지 않는다 (§15)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

from app import __version__

log = logging.getLogger("quant")


def _today() -> dt.date:
    return dt.date.today()


def _date(s: str | None) -> dt.date:
    return dt.date.fromisoformat(s) if s else _today()


# ------------------------------------------------------------------ 명령


def cmd_daily(args) -> int:
    from app.pipeline.daily import DailyRunner

    runner = DailyRunner(skip_guards=args.skip_guards)
    if args.catchup:
        outcomes = runner.catchup(_date(args.date), push=args.push)
        for o in outcomes:
            print(o.line())
        return 0 if all(o.ok for o in outcomes) else 1
    o = runner.run(_date(args.date), market=args.market, push=args.push)
    print(o.line())
    if o.summary:
        print(o.summary)
    return 0 if o.ok else 1


def cmd_backfill(args) -> int:
    from app.data.ingest import Ingestor
    from app.universe.snapshot import UniverseBuilder

    ing = Ingestor()
    end = _date(args.date)
    start = dt.date.fromisoformat(args.start) if args.start else None
    res = ing.backfill(end, start=start)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("halted"):
        return 1
    ub = UniverseBuilder()
    n = 0
    for d in UniverseBuilder.month_end_dates(start or dt.date(2015, 1, 31), end):
        n += ub.write(d)
    print(f"유니버스 스냅샷 {n}행 기록")
    return 0


def cmd_evaluate(args) -> int:
    """시드/제안 전략을 워크포워드로 평가하고 §7.5·§7.6 게이트를 적용한다."""
    import datetime as _dt

    from app.backtest.costs import CostModel
    from app.backtest.engine import PriceBook
    from app.backtest.walkforward import WalkForward
    from app.config import compute_protected_hash
    from app.data.meta_db import MetaDB
    from app.data.pit_store import PITStore
    from app.evaluator.gates import evaluate
    from app.evolve.curator import Curator
    from app.features.builder import FeatureBuilder
    from app.strategies.registry import all_strategies, control_ensemble, get_strategy
    from app.util.hashing import hash_dataframe

    end = _date(args.date)
    start = _dt.date.fromisoformat(args.start) if args.start else _dt.date(2016, 1, 1)
    store = PITStore()
    prices = store.prices(end, start=start)
    if prices.empty:
        print("가격 데이터가 없습니다. 먼저 `quant backfill` 를 실행하십시오.")
        return 1
    book = PriceBook(prices, store.fx(end, pair="USDKRW"))
    panel = FeatureBuilder(store).build(end, start=start)
    costs = CostModel.from_config()
    gates_hash = compute_protected_hash()
    data_hash = hash_dataframe(prices[["symbol", "event_date", "close"]])
    wf = WalkForward(panel, book, costs, gates_hash=gates_hash, data_hash=data_hash)

    targets = [get_strategy(args.strategy)] if args.strategy else [
        s for s in all_strategies() if not s.id.startswith(("bench_", "random_ctrl"))
    ]
    db, curator = MetaDB(), Curator()

    # 대조군은 한 번만 돌려서 모든 전략에 공유한다 (§7.5 게이트 5).
    print(f"무작위 대조군 {args.control_size}개 실행 중…")
    ctrl_e = []
    for c in control_ensemble(args.control_size):
        r = wf.run(c, start, end, require_min_folds=False)
        ctrl_e.append(float(r.combined_metrics.get("expectancy", float("nan"))))

    exit_code = 0
    for strat in targets:
        res = wf.run(strat, start, end, require_min_folds=not args.allow_short_history)
        k = db.next_k_index()
        db.upsert("multiple_testing", [{"k_index": k, "cycle_id": args.cycle or "manual",
                                        "strategy_id": strat.id,
                                        "created_at": _dt.datetime.now().isoformat(timespec="seconds")}])
        report = evaluate(
            res.combined_metrics, strategy_id=strat.id, family=str(strat.family),
            horizon_days=int(strat.horizon_days), k_index=k,
            control_expectancies=ctrl_e, fold_expectancies=res.fold_expectancies,
            is_sharpe=res.is_sharpe, oos_sharpe=res.oos_sharpe,
        )
        print()
        print(report.summary())
        db.upsert("strategy_runs", [{
            "run_id": f"{strat.id}-{end.isoformat()}-{k}", "strategy_id": strat.id,
            "version": str(strat.version), "fold": len(res.folds), "window": f"{start}~{end}",
            "metrics_json": json.dumps({**res.combined_metrics, "gates": report.to_dict()},
                                       ensure_ascii=False, default=str),
            "code_hash": res.test_results[0].code_hash if res.test_results else "",
            "data_hash": data_hash, "gates_hash": gates_hash,
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }])
        if args.promote:
            tr = curator.promote_to_candidate(report, str(strat.version))
            print(f"  → 상태 전이: {tr.before} → {tr.after} ({tr.reason})")
            db.log_evolution(ts=_dt.datetime.now().isoformat(timespec="seconds"),
                             action="state_transition", cycle_id=args.cycle,
                             before=tr.before, after=tr.after, reason=tr.reason)
        if not report.passed:
            exit_code = max(exit_code, 0)   # 게이트 실패는 정상 결과다. 오류가 아니다.
    return exit_code


def cmd_evolve(args) -> int:
    """§11.2 `evolve --if-pending`. 트리거가 없으면 즉시 종료 → 크레딧 소모 0."""
    import subprocess

    from app.data.meta_db import MetaDB
    from app.evolve.trigger import CONSENSUS_NOTE, read_pending
    from app.guards.llm_window import llm_window_guard
    from app.portfolio.killswitch import KillSwitch

    pending = read_pending()
    if args.if_pending and pending is None:
        print("trigger_pending.json 이 없습니다. 진화 조건 미충족 → 즉시 종료 (크레딧 소모 0).")
        print(CONSENSUS_NOTE)
        return 0
    if KillSwitch().tripped:
        print("킬스위치 발동 상태 — 진화 정지 (§8.2). 재개는 `quant resume --ack`.")
        return 1

    guard = llm_window_guard(force=args.force)
    print(str(guard))
    if not guard.ok:
        return 0   # 이월은 실패가 아니다

    if pending:
        print(f"진화 트리거 {len(pending.evolution_triggers)}건:")
        for t in pending.evolution_triggers:
            print(f"  - {t['code']} {t['target']}: {t['reason']}")

    if args.dry_run:
        print("--dry-run: Claude Code 헤드리스 호출을 생략합니다.")
        return 0

    cmd = ["claude", "-p", "/evolve", "--max-turns", str(args.max_turns)]
    print("실행:", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, check=False, timeout=args.timeout)
    except FileNotFoundError:
        msg = "claude CLI 를 찾을 수 없습니다. Claude Code 설치·로그인을 확인하십시오."
        print(msg)
        MetaDB().log_evolution(ts=dt.datetime.now().isoformat(timespec="seconds"),
                               action="evolve_failed", reason=msg)
        return 1
    except subprocess.TimeoutExpired:
        print("evolve 세션 타임아웃. 당일 재시도 없음 → 다음날 19:00 (§11.3).")
        return 1
    return proc.returncode


def cmd_lock(args) -> int:
    from app.config import check_protected_lock, update_lock

    if args.update:
        try:
            st = update_lock(args.ack, args.reason)
        except Exception as exc:
            print(str(exc))
            return 1
        print(f"protected.lock 갱신 완료: {st.actual}")
        return 0
    st = check_protected_lock()
    print(st.message)
    return 0 if st.ok else 1


def cmd_resume(args) -> int:
    """G1 킬스위치 해제 / G3 데이터 게이트 재개. --ack 없이는 아무것도 하지 않는다."""
    from app.data.meta_db import MetaDB
    from app.portfolio.killswitch import KillSwitch

    if not args.ack:
        print("사람 승인이 필요합니다. `quant resume --ack` 로 다시 실행하십시오 (G1/G3).")
        return 1
    ks = KillSwitch()
    st = ks.resume(True, args.note)
    MetaDB().log_evolution(ts=dt.datetime.now().isoformat(timespec="seconds"),
                           action="killswitch_resume", reason=args.note or "사람 승인(G1)")
    print(st.message())
    return 0


def cmd_healthcheck(args) -> int:
    from app.config import check_protected_lock
    from app.data.adapters.registry import active_source_labels, offline_mode
    from app.guards.clock import measure_drift
    from app.guards.host import current_hostname, domain_suffix
    from app.guards.network import active_proxies, current_ssid, dns_suffixes
    from app.guards.path import path_guard
    from app.portfolio.killswitch import KillSwitch
    from app.util.calendars import kr_calendar_verified

    if args.show_host:
        print(f"hostname: {current_hostname()}")
        print(f"도메인: {domain_suffix() or '(없음)'}")
        print("→ 이 값을 config/allowed_hosts.yaml 의 hostnames 에 등록하십시오.")
        return 0
    if args.show_network:
        print(f"SSID: {current_ssid() or '(확인 불가)'}")
        print(f"프록시: {sorted(active_proxies()) or '(없음)'}")
        print(f"DNS 접미사: {dns_suffixes() or '(없음)'}")
        print("→ SSID 를 config/allowed_networks.yaml 의 ssids 에 등록하십시오.")
        return 0

    drift, src = measure_drift()
    lock = check_protected_lock()
    ks = KillSwitch().read()
    lines = [
        f"버전: {__version__}",
        f"기기: {current_hostname()}",
        f"네트워크: SSID={current_ssid() or 'n/a'} 프록시={sorted(active_proxies()) or '없음'}",
        f"경로: {path_guard()}",
        f"시계: {'n/a' if drift is None else f'{drift:+.2f}s'} ({src})",
        f"protected.lock: {lock.message}",
        f"킬스위치: {ks.message()}",
        f"데이터 소스: {active_source_labels()} (offline={offline_mode()})",
        f"KR 휴장일 테이블 검증: {'예' if kr_calendar_verified() else '아니오 — 캘린더 게이트는 플래그로만 동작'}",
    ]
    print("\n".join(lines))
    return 0 if lock.ok and not ks.tripped else 1


def cmd_audit(args) -> int:
    """§11.8 / §17 기기 가시성 감사. 회사가 이 기기에서 무엇을 볼 수 있는가."""
    from app.guards.device import evaluate, policy, read_last, write_last
    from app.guards.device_audit import assess

    audit = assess()
    print(audit.render())
    print()

    cfg = policy()
    result = evaluate(audit, cfg, read_last())
    print(str(result))
    if not result.ok:
        print()
        print(f"  정책: max_level={cfg.get('max_level')}, "
              f"never_allow={cfg.get('never_allow')}, "
              f"예외={(cfg.get('overrides') or {}).get('allow_registered_without_mdm')}")

    if args.accept_baseline:
        if not audit.checked:
            print("Windows 가 아니라 기준선을 저장하지 않습니다.")
            return 1
        write_last(audit)
        print(f"\n현재 상태를 승격 감시 기준선으로 저장했습니다 (레벨 {int(audit.level)}).")
        print("이후 레벨이 올라가면 daily 가 자동으로 중단됩니다.")
    return 0 if result.ok else 1


def cmd_replay(args) -> int:
    """§11.7 당시 데이터·코드·게이트 해시로 결정 재현. 재현 불가 = 버그."""
    from app.data.meta_db import MetaDB
    from app.pipeline.daily import DailyRunner

    date = _date(args.date)
    db = MetaDB()
    prev = db.query("SELECT result_hash FROM run_log WHERE run_date = ? AND task = 'daily'",
                    (date.isoformat(),))
    if prev.empty:
        print(f"{date} 실행 기록이 없습니다.")
        return 1
    expected = str(prev.iloc[0]["result_hash"])
    outcome = DailyRunner(skip_guards=True).run(date, mode="재현")
    ok = outcome.result_hash == expected
    print(f"기대 {expected} / 재현 {outcome.result_hash} → {'일치' if ok else '불일치 (버그)'}")
    return 0 if ok else 1


def cmd_calendar(args) -> int:
    from app.util.calendars import kr_calendar_verified, refresh_kr_from_pykrx

    if args.refresh_kr:
        try:
            n = refresh_kr_from_pykrx(args.from_year, args.to_year)
        except ImportError:
            print("pykrx 가 설치되어 있지 않습니다: uv pip install -e .[market]")
            return 1
        print(f"KR 휴장일 테이블 {n}개 연도 재생성 완료 (source=pykrx)")
        return 0
    print(f"KR 휴장일 테이블 검증 상태: {'검증됨' if kr_calendar_verified() else '미검증(시드값)'}")
    return 0


def cmd_weekly(args) -> int:
    from app.reports.periodic import write_weekly

    p = write_weekly(_date(args.date))
    print(f"주간 리포트: {p}")
    return 0


def cmd_monthly(args) -> int:
    from app.reports.periodic import write_monthly

    p = write_monthly(_date(args.date))
    print(f"월간 리포트: {p}")
    return 0


# ------------------------------------------------------------------ 파서


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quant", description="자가 발전형 시장 리서치 (페이퍼 전용)")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("daily", help="§11.4 일일 파이프라인 (LLM 호출 없음)")
    d.add_argument("--date")
    d.add_argument("--market", choices=["kr", "us", "KR", "US"])
    d.add_argument("--catchup", action="store_true", help="놓친 날 전부 순서대로 보충")
    d.add_argument("--push", action="store_true", help="완료 후 git push")
    d.add_argument("--skip-guards", action="store_true", help="개발 전용. 실운영 금지")
    d.set_defaults(func=cmd_daily)

    b = sub.add_parser("backfill", help="과거 데이터 적재 (Phase 0)")
    b.add_argument("--date")
    b.add_argument("--start")
    b.set_defaults(func=cmd_backfill)

    e = sub.add_parser("evaluate", help="워크포워드 + §7.5·§7.6 게이트")
    e.add_argument("--date")
    e.add_argument("--start")
    e.add_argument("--strategy")
    e.add_argument("--cycle")
    e.add_argument("--control-size", type=int, default=20,
                   help="대조군 앙상블 크기 (명세 기준 100, 기본값은 실행 시간 고려)")
    e.add_argument("--promote", action="store_true", help="게이트 통과 시 candidate 로 전이")
    e.add_argument("--allow-short-history", action="store_true", help="폴드 10개 미만 허용 (개발용)")
    e.set_defaults(func=cmd_evaluate)

    v = sub.add_parser("evolve", help="§10 진화 사이클 (Claude Code 헤드리스)")
    v.add_argument("--if-pending", action="store_true")
    v.add_argument("--force", action="store_true", help="LLM 창 제한 무시 (§11.3)")
    v.add_argument("--dry-run", action="store_true")
    v.add_argument("--max-turns", type=int, default=40)
    v.add_argument("--timeout", type=int, default=3600)
    v.set_defaults(func=cmd_evolve)

    lk = sub.add_parser("lock", help="protected.lock 검사/갱신 (G2)")
    lk.add_argument("--update", action="store_true")
    lk.add_argument("--ack", action="store_true", help="사람 승인")
    lk.add_argument("--reason", default="")
    lk.set_defaults(func=cmd_lock)

    r = sub.add_parser("resume", help="킬스위치·데이터 게이트 해제 (G1/G3)")
    r.add_argument("--ack", action="store_true")
    r.add_argument("--data", action="store_true", help="데이터 게이트 중단 해제 (G3)")
    r.add_argument("--note", default="")
    r.set_defaults(func=cmd_resume)

    h = sub.add_parser("healthcheck", help="가드·상태 점검")
    h.add_argument("--show-host", action="store_true")
    h.add_argument("--show-network", action="store_true")
    h.set_defaults(func=cmd_healthcheck)

    a = sub.add_parser("audit", help="§11.8 기기 가시성 감사 (회사가 무엇을 볼 수 있는가)")
    a.add_argument("--device", action="store_true", help="기기 감사 (기본 동작)")
    a.add_argument("--accept-baseline", action="store_true",
                   help="현재 상태를 승격 감시 기준선으로 저장")
    a.set_defaults(func=cmd_audit)

    rp = sub.add_parser("replay", help="§11.7 결정 재현")
    rp.add_argument("--date", required=True)
    rp.set_defaults(func=cmd_replay)

    c = sub.add_parser("calendar", help="휴장일 테이블 관리")
    c.add_argument("--refresh-kr", action="store_true")
    c.add_argument("--from", dest="from_year", type=int, default=2013)
    c.add_argument("--to", dest="to_year", type=int, default=dt.date.today().year)
    c.set_defaults(func=cmd_calendar)

    w = sub.add_parser("weekly", help="§13.2 주간 리포트")
    w.add_argument("--date")
    w.set_defaults(func=cmd_weekly)

    m = sub.add_parser("monthly", help="§13.3 월간 리포트")
    m.add_argument("--date")
    m.set_defaults(func=cmd_monthly)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("중단됨")
        return 130


if __name__ == "__main__":
    sys.exit(main())
