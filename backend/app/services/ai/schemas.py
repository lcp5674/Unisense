"""AI 问数 Schemas（TD §12.7 / FR-14）。"""

from __future__ import annotations

from pydantic import BaseModel


class NL2SQLRequest(BaseModel):
    nl_query: str
    metric_scope: list[str] | None = None
    execute: bool = False


class NL2SQLResponse(BaseModel):
    nl_query: str
    anchored_entities: list[str]
    sql: str
    safe: bool
    notes: list[str]

    @classmethod
    def build(
        cls, nl_query: str, anchored: list[str], sql: str, safe: bool, notes: list[str]
    ) -> NL2SQLResponse:
        return cls(
            nl_query=nl_query,
            anchored_entities=anchored,
            sql=sql,
            safe=safe,
            notes=notes,
        )
