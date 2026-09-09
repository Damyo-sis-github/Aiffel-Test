"""진화 산출물이 들어오는 곳. `/evolve` 가 **쓸 수 있는 유일한 전략 디렉터리**다.

여기 있는 `StrategyBase` 하위 클래스는 `registry._proposed_strategies()` 가 자동
수집한다. 그러려면 이 디렉터리가 **패키지여야** 한다 — `__init__.py` 가 없으면
`importlib.import_module("app.strategies.proposed")` 가 실패하고, registry 는 그걸
ModuleNotFoundError 로 삼켜 빈 튜플을 돌려준다. 즉 LLM 이 파일을 아무리 잘 써도
아무 일도 일어나지 않는다. 조용히.

파일을 지우지 마라. 반려된 전략의 코드도 남긴다 (§7.6 — 실패 로그가 자산이다).
"""
