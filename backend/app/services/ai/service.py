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

import copy
import hashlib
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_service import BaseService
from app.core.exceptions import UnisenseError
from app.core.logging import get_logger
from app.services.ai.repository import AiRepository
from app.services.llm.client import LlmClient, LlmError, build_llm_client

logger = get_logger(__name__)

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

#: NL2SQL 结果缓存 TTL（秒）。相同问句重复调用时直接命中缓存，
#: 避免反复打外部 LLM 网关（OpenAI 协议兼容网关延迟通常 >1s，导致并发 P95 超标）。
_CACHE_TTL = 60


class AiService(BaseService):
    """AI 问数服务：NL2SQL + 安全约束 + 执行委托。"""

    def __init__(self, session: AsyncSession, llm: LlmClient | None = None) -> None:
        super().__init__(session)
        self._session = session
        self._repo = AiRepository(session)
        self._llm = llm or build_llm_client()
        #: 进程内 TTL 缓存：key -> (monotonic 时间戳, 结果 dict)。单实例部署下命中一致。
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def _is_unsafe(self, text: str) -> bool:
        """检查文本是否包含危险模式。"""
        lowered = text.lower()
        return any(pattern in lowered for pattern in _BANNED_PATTERNS)

    @staticmethod
    def _cache_key(nl_query: str, metric_scope: list[str] | None) -> str:
        """构造缓存键：问句 + 排序后的指标范围（相同输入命中同一缓存）。"""
        scope = "|".join(sorted(metric_scope or []))
        return hashlib.sha256(f"{nl_query}|{scope}".encode()).hexdigest()

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        """读取未过期的缓存结果（返回深拷贝，避免调用方修改污染缓存）。"""
        hit = self._cache.get(key)
        if hit is None:
            return None
        ts, value = hit
        if time.monotonic() - ts >= _CACHE_TTL:
            self._cache.pop(key, None)
            return None
        return copy.deepcopy(value)

    def clear_cache(self) -> None:
        """清空进程内缓存（运维/测试用）。"""
        self._cache.clear()

    async def nl2sql(self, nl_query: str, metric_scope: list[str] | None = None) -> dict[str, Any]:
        """将自然语言查询转换为 SQL。

        优先使用 LLM 生成 SQL，失败时降级为关键词匹配。
        返回结果包含参数化 SQL + params 字典。
        """
        if self._is_unsafe(nl_query):
            raise UnisenseError(
                "查询包含危险语句，已拒绝",
                error_code="UNSAFE_QUERY",
            )

        # 缓存命中：相同问句 + 相同范围，直接返回上次结果（不重复打外部 LLM 网关）
        cache_key = self._cache_key(nl_query, metric_scope)
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        # 获取词汇表用于锚定
        vocab = await self._repo.vocabulary()
        if metric_scope:
            vocab = vocab & set(metric_scope)

        # 尝试使用 LLM 生成 SQL
        sql = await self._generate_sql_with_llm(nl_query, vocab)
        params: dict[str, Any] = {}

        if sql is None or sql.strip() == "":
            # 降级为关键词匹配（参数化）
            sql, params = await self._generate_sql_with_keywords(nl_query, vocab)

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
            result = {
                "anchored": [],
                "sql": "",
                "params": {},
                "safe": False,
                "notes": ["未锚定到已知指标/术语，请使用已注册的名称"],
                "method": "none",
            }
            # 写缓存存深拷贝：返回对象与缓存隔离，调用方修改不污染缓存
            self._cache[cache_key] = (time.monotonic(), copy.deepcopy(result))
            return result

        result = {
            "anchored": anchors,
            "sql": sql,
            "params": params,
            "safe": True,
            "notes": [],
            "method": "llm" if sql != "" else "keyword",
        }
        # 写缓存（含降级结果：keyword 匹配结果稳定，命中同样省去重复计算）；存深拷贝防污染
        self._cache[cache_key] = (time.monotonic(), copy.deepcopy(result))
        return result

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
    ) -> tuple[str, dict[str, Any]]:
        """使用关键词匹配生成参数化 SQL（降级方案）。

        Returns:
            (sql, params) 元组：参数化 SQL 和参数字典。
        """
        anchors = [v for v in vocab if v.lower() in nl_query.lower()]
        if not anchors:
            return "", {}

        # 参数化 SQL 模板：使用 :param 占位符而非 f-string 拼接
        columns = ", ".join(anchors[:5])  # 限制列数
        where_conditions = " AND ".join(
            [f"metric_code = :metric_code_{i}" for i in range(min(len(anchors), 3))]
        )
        sql = f"SELECT {columns} FROM unified_metric WHERE {where_conditions}"

        # 构建参数字典
        params: dict[str, Any] = {}
        for i, anchor in enumerate(anchors[:3]):
            params[f"metric_code_{i}"] = anchor

        logger.info("关键词匹配 SQL 生成（参数化），锚定词=%d", len(anchors))
        return sql, params

    async def ask(
        self,
        nl_query: str,
        execute: bool = False,
        metric_scope: list[str] | None = None,
    ) -> dict[str, Any]:
        """问数接口：NL2SQL + 可选执行。

        Args:
            nl_query: 自然语言查询
            execute: 是否执行生成的 SQL
            metric_scope: 指标范围限制

        Returns:
            包含 SQL、锚定词、安全状态的结果；execute=True 时含执行结果
        """
        result = await self.nl2sql(nl_query, metric_scope)
        result["execute"] = execute
        if execute and result.get("sql"):
            # 委托 consume 服务执行 SQL
            try:
                from app.services.consume.olap_executor import OLAPExecutor

                executor = OLAPExecutor()
                sql = result["sql"]
                params = result.get("params", {})
                olap_result = await executor.execute(sql, params)
                result["execute_result"] = {
                    "rows": olap_result.rows,
                    "total": olap_result.total,
                    "elapsed_ms": olap_result.elapsed_ms,
                }
            except Exception as exc:
                result["execute_error"] = str(exc)
                logger.warning("ai_ask_execute_failed", error=str(exc), exc_info=True)
        return result

    async def close(self) -> None:
        """关闭 LLM 客户端连接。"""
        if isinstance(self._llm, LlmClient):
            await self._llm.close()
