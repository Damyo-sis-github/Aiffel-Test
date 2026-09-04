"""§8.2 킬스위치. 포트 MDD 15% → 전 신호 정지 + 진화 정지 + 텔레그램.

재개는 사람 게이트 G1 (`quant resume --ack`) 로만. 자동 해제 경로는 존재하지 않는다 (#13).
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from app.config import risk as load_risk
from app.paths import state_dir

STATE_FILE = "killswitch.json"


@dataclass(frozen=True)
class KillSwitchState:
    tripped: bool
    tripped_at: str | None = None
    account: str | None = None
    drawdown: float | None = None
    threshold: float | None = None
    note: str = ""

    def message(self) -> str:
        if not self.tripped:
            return "킬스위치: 정상"
        return (
            f"🛑 킬스위치 발동 ({self.tripped_at}) — {self.account} 낙폭 "
            f"{(self.drawdown or 0):.1%} > 한도 {(self.threshold or 0):.1%}. "
            "전 신호·진화 정지. 재개는 `quant resume --ack` (사람 게이트 G1)."
        )


class KillSwitch:
    def __init__(self, path: Path | None = None, cfg: dict | None = None):
        self.path = path or (state_dir() / STATE_FILE)
        self.cfg = (cfg or load_risk())["killswitch"]
        self.threshold = float(self.cfg["portfolio_mdd_pct"])

    def read(self) -> KillSwitchState:
        if not self.path.exists():
            return KillSwitchState(tripped=False)
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # 손상된 상태 파일은 "발동"으로 본다 (fail-closed).
            return KillSwitchState(tripped=True, note="킬스위치 상태 파일 손상 → 안전측 정지")
        return KillSwitchState(**d)

    @property
    def tripped(self) -> bool:
        return self.read().tripped

    def evaluate(self, drawdown: float, account: str, when: dt.date | None = None) -> KillSwitchState:
        """drawdown 은 양수(낙폭 크기). 한도 초과 시 상태를 디스크에 남긴다."""
        st = self.read()
        if st.tripped:
            return st
        if drawdown is None or drawdown != drawdown or drawdown <= self.threshold:
            return st
        new = KillSwitchState(
            tripped=True,
            tripped_at=(when or dt.date.today()).isoformat(),
            account=account,
            drawdown=float(drawdown),
            threshold=self.threshold,
            note="§8.2 포트 MDD 한도 초과",
        )
        self._write(new)
        return new

    def resume(self, ack: bool, note: str = "") -> KillSwitchState:
        """G1 사람 게이트. ack 없이는 절대 해제되지 않는다."""
        if not ack:
            raise PermissionError(
                "킬스위치 해제는 사람 승인이 필요합니다: `quant resume --ack` (G1). "
                "자동 해제 경로는 존재하지 않습니다."
            )
        new = KillSwitchState(tripped=False, note=note or "사람 승인으로 해제 (G1)")
        self._write(new)
        return new

    def _write(self, st: KillSwitchState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(st.__dict__, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
