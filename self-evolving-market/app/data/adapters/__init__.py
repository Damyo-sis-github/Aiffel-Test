"""데이터 소스 어댑터. §4.1

모든 어댑터는 `PriceSource` / `MacroSource` 계약을 지키고, 반환 DataFrame 은
`app.data.schema` 의 컬럼 계약을 만족해야 한다 (#13 소스 스키마 변경 방어).
"""

from app.data.adapters.base import MacroSource, PriceSource, SourceUnavailable
from app.data.adapters.registry import get_macro_source, get_price_source

__all__ = ["MacroSource", "PriceSource", "SourceUnavailable", "get_macro_source", "get_price_source"]
