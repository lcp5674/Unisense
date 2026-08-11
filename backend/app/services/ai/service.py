"""AI 问数服务（TD §12.7 / FR-14）。

核心能力：
1. NL2SQL：使用 LLM 将自然语言转换为 SQL（带安全约束）
2. 语义锚定：将查询锚定到已注册的指标/术语词汇表
3. 防注入：强制安全约束，禁止危险操作
4. 执行委托：生成 SQL 后委托 consume 服务执行

支持配置：
  UNISENSE_LLM_PROVIDER=openai|deepseek|kilo  # 提供商
  UNISENSE_LLM_BASE_URL=https://api.deepseek.com
  UNISENSE_LLM_API_KEY=sk-xxx
  UNISENSE_LLM_MODEL=deepseek-chat
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UnisenseError
from app.services.ai.repository import AiRepository
from app.services.llm.client import LlmClient, LlmError, build_llm_client

logger = logging.getLogger(__name__)

# 安全约束：禁止的 SQL 模式
_BANNED_PATTERNS = (
    "select *",
    "delete ",
    "drop ",
    "update ",
    "insert ",
    "truncate ",
    "alter ",
    "execute ",
    "call ",
    ";",
    "--",
    "/*",
    "union all",
    "union select",
)


class AiService:
    """AI 问数服务：NL2SQL + 安全约束 + 执行委托。"""

    def __init__(self, session: AsyncSession, llm: LlmClient | None = None) -> None:
        self._session = session
        self._repo = AiRepository(session)
        self._llm = llm or build_llm_client()

    def _is_unsafe(self, text: str) -> bool:
        """检查文本是否包含危险模式。"""
        lowered = text.lower()
        return any(pattern in lowered for pattern in _BANNED_PATTERNS)

    async def nl2sql(self, nl_query: str, metric_scope: list[str] | None = None) -> dict[str, Any]:
        """将自然语言查询转换为 SQL。

        优先使用 LLM 生成 SQL，失败时降级为关键词匹配。
        """
        if self._is_unsafe(nl_query):
            raise UnisenseError(
                "查询包含危险语句，已拒绝",
                error_code="UNSAFE_QUERY",
            )

        # 获取词汇表用于锚定
        vocab = await self._repo.vocabulary()
        if metric_scope:
            vocab = vocab & set(metric_scope)

        # 尝试使用 LLM 生成 SQL
        sql = await self._generate_sql_with_llm(nl_query, vocab)

        if sql is None or sql.strip() == "":
            # 降级为关键词匹配
            sql = await self._generate_sql_with_keywords(nl_query, vocab)

        # 安全校验
        if self._is_unsafe(sql):
            raise UnisenseError(
                "生成的 SQL 包含危险语句，已拒绝",
                error_code="UNSAFE_QUERY",
            )

        # 提取锚定词
        anchors = [v for v in vocab if v.lower() in nl_query.lower()]

        # 没有锚定词且未生成 SQL：明确标记不安全（未知指标）
        if not anchors and not sql.strip():
            return {
                "anchored": [],
                "sql": "",
                "safe": False,
                "notes": ["未锚定到已知指标/术语，请使用已注册的名称"],
                "method": "none",
            }

        return {
            "anchored": anchors,
            "sql": sql,
            "safe": True,
            "notes": [],
            "method": "llm" if sql != "" else "keyword",
        }

    async def _generate_sql_with_llm(
        self, nl_query: str, vocab: set[str]
    ) -> str | None:
        """使用 LLM 生成 SQL。"""
        if not self._llm.enabled:
            return None

        # 构建词汇表提示
        vocab_sample = list(vocab)[:20]  # 限制大小
        vocab_str = ", ".join(vocab_sample)

        prompt = f"""你是数据中台的 SQL 生成助手。用户想查询数据，请用 SQL 表达其意图。

可用指标/术语（部分）：{vocab_str}

用户查询：{nl_query}

要求：
1. 仅生成 SELECT 语句，禁止 DML/DDL
2. 禁止 SELECT *，必须指定列名
3. 从以下指标中选择最相关的：{', '.join(vocab_sample)}
4. 生成标准 SQL，使用统一视图 unified_metric
5. 仅返回 SQL，不要解释

示例输出：
SELECT metric_code, value FROM unified_metric
WHERE metric_code = 'sales_gmv_daily' AND dt = '2024-01-01'
"""

        try:
            result = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=500,
            )
            content = result.get("content", "").strip()
            # 提取 SQL（去除可能的 markdown 代码块）
            content = str(result.get("content", "")).strip()
            if "```sql" in content:
                content = content.split("```sql")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            # 确保以 SELECT 开头
            if content.upper().startswith("SELECT"):
                logger.info("LLM SQL 生成成功，长度=%d", len(content))
                return content
            return None
        except LlmError as exc:
            logger.warning("LLM SQL 生成失败，降级为关键词匹配: %s", exc)
            return None

    async def _generate_sql_with_keywords(
        self, nl_query: str, vocab: set[str]
    ) -> str:
        """使用关键词匹配生成 SQL（降级方案）。"""
        anchors = [v for v in vocab if v.lower() in nl_query.lower()]
        if not anchors:
            return ""

        # 简单的 SQL 模板
        columns = ", ".join(anchors[:5])  # 限制列数
        where_conditions = " AND ".join(
            [f"metric_code = '{a}'" for a in anchors[:3]]
        )
        sql = f"SELECT {columns} FROM unified_metric WHERE {where_conditions}"
        logger.info("关键词匹配 SQL 生成，锚定词=%d", len(anchors))
        return sql

    async def ask(
        self,
        nl_query: str,
        execute: bool = False,
        metric_scope: list[str] | None = None,
    ) -> dict[str, Any]:
        """问数接口：NL2SQL + 可选执行。

        Args:
            nl_query: 自然语言查询
            execute: 是否执行生成的 SQL（当前仅返回 SQL，不执行）
            metric_scope: 指标范围限制

        Returns:
            包含 SQL、锚定词、安全状态的结果
        """
        result = await self.nl2sql(nl_query, metric_scope)
        result["execute"] = execute
        if execute:
            result["notes"].append(
                "SQL 已生成，委托 consume 服务执行（/api/v1/consume/query）"
            )
        return result

    async def close(self) -> None:
        """关闭 LLM 客户端连接。"""
        if isinstance(self._llm, LlmClient):
            await self._llm.close()
