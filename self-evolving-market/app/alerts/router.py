"""§11.6 알림. 텔레그램 1차, 카카오톡 2차.

전송 실패는 alerts 테이블에 기록하고 다음 실행에서 재전송한다.
자격증명은 .env 에서만 읽고 메시지·로그·커밋에 싣지 않는다 (#17).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from enum import StrEnum

import pandas as pd

from app.config import alerts_cfg
from app.data.meta_db import MetaDB
from app.paths import reports_dir

log = logging.getLogger(__name__)


class Level(StrEnum):
    CRITICAL = "critical"
    ERROR = "error"
    WARN = "warn"
    INFO = "info"


class AlertRouter:
    def __init__(self, db: MetaDB | None = None, cfg: dict | None = None):
        self.db = db or MetaDB()
        self.cfg = cfg or alerts_cfg()

    # ------------------------------------------------------------ 전송

    def send(self, level: Level, message: str, *, channels: list[str] | None = None) -> dict[str, bool]:
        chans = channels or self._channels_for(level)
        results: dict[str, bool] = {}
        for name in chans:
            ok = self._deliver(name, level, message)
            results[name] = ok
            self._record(level, name, message, ok)
            fb = (self.cfg["channels"].get(name, {}) or {}).get("fallback_channel")
            if not ok and fb and fb not in results:
                ok_fb = self._deliver(fb, level, f"[{name} 실패 대체] {message}")
                results[fb] = ok_fb
                self._record(level, fb, message, ok_fb)
        return results

    def critical(self, message: str) -> dict[str, bool]:
        return self.send(Level.CRITICAL, message)

    def info(self, message: str) -> dict[str, bool]:
        return self.send(Level.INFO, message)

    def retry_failed(self, limit: int = 50) -> int:
        """지난 실행에서 실패한 알림 재전송.

        콘솔 전용 모드에서는 재전송하지 않는다 — 다시 콘솔에 찍어봤자 같은 곳으로 사라지고,
        매 실행마다 밀린 알림을 전부 다시 뱉는 소음만 난다. 대기 상태로 남겨두고
        `quant healthcheck` 가 개수를 보여준다.
        """
        if self.console_only():
            return 0
        rows = self.db.query(
            "SELECT id, level, channel, message FROM alerts WHERE delivered = 0 "
            "ORDER BY id LIMIT ?", (limit,)
        )
        sent = 0
        for _, r in rows.iterrows():
            if self._deliver(str(r["channel"]), Level(str(r["level"])), str(r["message"])):
                self.db.query("UPDATE alerts SET delivered = 1 WHERE id = ?", (int(r["id"]),))
                sent += 1
        return sent

    def pending(self, levels: tuple[str, ...] = ("critical", "error")) -> pd.DataFrame:
        """아직 사람에게 닿지 못한 알림. healthcheck 가 이걸 보여준다."""
        marks = ",".join("?" * len(levels))
        return self.db.query(
            f"SELECT ts, level, channel, message FROM alerts "  # noqa: S608 - 플레이스홀더만 조립
            f"WHERE delivered = 0 AND level IN ({marks}) ORDER BY id DESC LIMIT 20",
            levels,
        )

    def console_only(self) -> bool:
        return bool(self.cfg.get("offline_console_only", True))

    # ------------------------------------------------------------ 내부

    def _channels_for(self, level: Level) -> list[str]:
        out = []
        for name, c in (self.cfg.get("channels") or {}).items():
            if c.get("enabled") and level.value in (c.get("levels") or []):
                out.append(name)
        return out or ["console"]

    def _record(self, level: Level, channel: str, message: str, ok: bool) -> None:
        self.db.upsert(
            "alerts",
            [{"ts": dt.datetime.now().isoformat(timespec="seconds"), "level": level.value,
              "channel": channel, "message": message[:4000], "delivered": int(ok), "attempts": 1}],
        )

    def _append_log(self, level: Level, channel: str, message: str) -> None:
        """모든 알림을 파일에도 남긴다.

        작업 스케줄러로 돌면 stdout 은 어디에도 남지 않는다. 콘솔 출력만 믿으면
        킬스위치·기기 승격 같은 치명적 알림이 **아무도 모르게 사라진다.**
        """
        path = reports_dir() / str(self.cfg.get("log_file", "alerts.log"))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                ts = dt.datetime.now().isoformat(timespec="seconds")
                f.write(f"[{ts}] [{level.value.upper():8s}] [{channel}] {message}\n")
        except OSError as exc:  # 로그 실패가 daily 를 죽이면 안 된다
            log.warning("알림 로그 기록 실패: %s", type(exc).__name__)

    def _deliver(self, channel: str, level: Level, message: str) -> bool:
        # 어떤 경로든 파일 로그는 항상 남긴다.
        self._append_log(level, channel, message)

        if self.console_only() or channel == "console":
            print(f"[알림/{channel}/{level.value}] {message}")
            # 콘솔은 "전달"이 아니다. info 만 전달로 치고, critical/error/warn 은
            # 미전달로 남겨 실제 채널이 생겼을 때 다시 보내고, healthcheck 에도 뜬다.
            counted = {str(x) for x in (self.cfg.get("console_counts_as_delivery_for") or ["info"])}
            return level.value in counted

        try:
            if channel == "telegram":
                return _send_telegram(message, int((self.cfg["channels"]["telegram"] or {}).get("max_lines", 10)))
            if channel == "kakao":
                return _send_kakao(message)
        except Exception as exc:  # 알림 실패가 daily 를 죽이면 안 된다
            log.warning("알림 전송 실패 (%s): %s", channel, type(exc).__name__)
            return False
        print(f"[알림/{channel}/{level.value}] {message}")
        return True


def _truncate(message: str, max_lines: int) -> str:
    lines = message.splitlines()
    if len(lines) <= max_lines:
        return message
    return "\n".join(lines[:max_lines]) + f"\n… (+{len(lines) - max_lines}줄, 자세한 내용은 md 리포트)"


def _send_telegram(message: str, max_lines: int = 10) -> bool:
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 .env 에 없습니다.")
    import httpx

    resp = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": _truncate(message, max_lines), "disable_web_page_preview": True},
        timeout=10.0,
    )
    return resp.status_code == 200


def _send_kakao(message: str) -> bool:
    token = os.environ.get("KAKAO_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("KAKAO_ACCESS_TOKEN 이 .env 에 없습니다.")
    import json as _json

    import httpx

    resp = httpx.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {token}"},
        data={"template_object": _json.dumps(
            {"object_type": "text", "text": message[:2000],
             "link": {"web_url": "https://github.com"}}, ensure_ascii=False)},
        timeout=10.0,
    )
    return resp.status_code == 200
