"""결정론적 해시. §13.4 채택마다 code/data/gates 해시 삼중 기록.

주의: 여기 함수들은 부동소수를 문자열로 정규화한 뒤 해시한다.
같은 입력 → 같은 해시가 플랫폼 간에도 유지되어야 하기 때문이다(#11 멱등성 테스트).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

FLOAT_NDIGITS = 10


def _canon(obj: Any) -> Any:
    """JSON 직렬화 가능한 정규형으로 변환. dict 키는 정렬, float 은 반올림."""
    if isinstance(obj, float):
        # -0.0 과 0.0 을 같게, NaN 을 안정적으로.
        if obj != obj:  # NaN
            return "NaN"
        r = round(obj, FLOAT_NDIGITS)
        return 0.0 if r == 0 else r
    if isinstance(obj, Decimal):
        return _canon(float(obj))
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return obj.as_posix()
    if isinstance(obj, dict):
        return {str(k): _canon(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    if isinstance(obj, set | frozenset):
        return [_canon(v) for v in sorted(obj, key=repr)]
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if hasattr(obj, "to_dict"):
        return _canon(obj.to_dict())
    return repr(obj)


def hash_obj(obj: Any) -> str:
    """임의 파이썬 객체의 결정론적 SHA-256 (앞 16자)."""
    payload = json.dumps(_canon(obj), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def hash_paths(paths: Iterable[Path]) -> str:
    """여러 파일/디렉터리의 합성 해시. 경로는 정렬되어 결정론적이다."""
    parts: list[tuple[str, str]] = []
    for p in sorted({Path(x) for x in paths}, key=lambda x: x.as_posix()):
        if p.is_dir():
            for f in sorted(p.rglob("*.py"), key=lambda x: x.as_posix()):
                if "__pycache__" in f.parts:
                    continue
                parts.append((f.as_posix(), hash_file(f)))
        elif p.is_file():
            parts.append((p.as_posix(), hash_file(p)))
        else:
            parts.append((p.as_posix(), "<missing>"))
    return hash_obj(parts)


def hash_dataframe(df) -> str:
    """pandas DataFrame 의 결정론적 해시. 컬럼·인덱스 정렬 후 계산."""

    if df is None or len(df) == 0:
        return hash_obj({"empty": True, "cols": list(getattr(df, "columns", []))})
    d = df.copy()
    d = d[sorted(d.columns)]
    sort_cols = [c for c in d.columns if d[c].dtype.kind in "OMiub" or c in ("date", "symbol")]
    if sort_cols:
        d = d.sort_values(sort_cols, kind="mergesort")
    records = []
    for row in d.itertuples(index=False, name=None):
        records.append([_canon(v.item() if hasattr(v, "item") else v) for v in row])
    return hash_obj({"cols": list(d.columns), "rows": records, "n": int(len(d))})
