"""
配置管理模块

收拢环境配置、运行配置和功能开关，减少主链路里的硬编码默认值。
"""

from __future__ import annotations

import copy
import os

from dotenv import load_dotenv

load_dotenv()


def _build_default_db_source() -> dict[str, object]:
    return {
        "driver": os.getenv("DB_DRIVER", "mysql"),
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", 3307)),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", "123456"),
        "database": os.getenv("DB_NAME", "chatbi_mvp"),
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", 5)),
        "read_timeout": int(os.getenv("DB_READ_TIMEOUT", 8)),
        "write_timeout": int(os.getenv("DB_WRITE_TIMEOUT", 8)),
    }


APP_CONFIG = {
    "environment": os.getenv("APP_ENV", "dev"),
    "database": {
        "default_source": os.getenv("DB_DEFAULT_SOURCE", "mysql_main"),
        "sources": {
            os.getenv("DB_DEFAULT_SOURCE", "mysql_main"): _build_default_db_source(),
        },
        "runtime": {
            "pool_size": int(os.getenv("DB_POOL_SIZE", 5)),
            "max_overflow": int(os.getenv("DB_POOL_MAX_OVERFLOW", 5)),
            "pool_timeout": float(os.getenv("DB_POOL_TIMEOUT", 3)),
            "slow_query_threshold_ms": float(os.getenv("DB_SLOW_QUERY_THRESHOLD_MS", 200)),
        },
    },
    "llm": {
        "api_key": os.getenv("OPENAI_API_KEY"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model": os.getenv("LLM_MODEL", "gpt-4"),
        "embedding_model": os.getenv("EMBEDDING_MODEL", "text-embedding-3-large"),
        "temperature": 0.1,
        "max_tokens": int(os.getenv("LLM_MAX_TOKENS", 4000)),
    },
    "features": {
        "few_shot": os.getenv("FEATURE_FEW_SHOT", "true").lower() == "true",
        "rules": os.getenv("FEATURE_RULES", "true").lower() == "true",
        "guards": os.getenv("FEATURE_GUARDS", "true").lower() == "true",
        "indicator_knowledge": os.getenv("FEATURE_INDICATOR_KNOWLEDGE", "true").lower() == "true",
        "schema_linking": os.getenv("FEATURE_SCHEMA_LINKING", "true").lower() == "true",
        "indicator_rag": os.getenv("FEATURE_INDICATOR_RAG", "false").lower() == "true",
    },
    # ---- 安全（SQL 校验 / 行级权限 / 令牌）----
    "security": {
        # 返回行数上限：无 LIMIT 自动补齐，超过上限压到上限
        "max_rows": int(os.getenv("MAX_ROWS", 1000)),
        # 会话级执行超时（毫秒），每次查询前 SET SESSION max_execution_time
        "query_timeout_ms": int(os.getenv("QUERY_TIMEOUT_MS", 30000)),
        # 单条查询允许引用的最大表数 / 子查询嵌套深度
        "max_tables_per_query": int(os.getenv("MAX_TABLES_PER_QUERY", 6)),
        "max_subquery_depth": int(os.getenv("MAX_SUBQUERY_DEPTH", 5)),
        # 表白名单、函数黑名单、敏感列等策略文件路径（相对项目根目录）
        "policy_path": os.getenv("SECURITY_POLICY_PATH", "security_policy.json"),
        # 表列元数据（供 ops 角色 SELECT * 展开非敏感列）
        "table_columns_path": os.getenv("TABLE_COLUMNS_PATH", "field_metadata.json"),
        # 令牌签名密钥（HMAC-SHA256），生产必配
        "auth_secret": os.getenv("AUTH_SECRET", ""),
        # 开发模式：true 时允许无令牌访问并以 demo_admin 身份执行；默认关闭
        "allow_demo_admin": os.getenv("ALLOW_DEMO_ADMIN", "false").lower() == "true",
        # dim_stores.owner -> store_id 映射缓存 TTL（秒），默认 10 分钟
        "store_cache_ttl_seconds": int(os.getenv("STORE_CACHE_TTL_SECONDS", 600)),
    },
    # 仅作回显；实际开关由 langsmith 运行期读 LANGSMITH_TRACING 等环境变量决定
    "tracing": {
        "enabled": os.getenv(
            "LANGSMITH_TRACING", os.getenv("LANGCHAIN_TRACING", "false")
        ).lower()
        == "true",
        "project": os.getenv(
            "LANGSMITH_PROJECT", os.getenv("LANGCHAIN_PROJECT", "chatbi")
        ),
    },
}


def get_database_source_config(
    source_id: str | None = None,
    app_config: dict | None = None,
) -> dict[str, object]:
    """返回指定数据源的配置，默认取当前应用默认数据源。"""
    config = app_config or APP_CONFIG
    database_config = config["database"]
    resolved_source_id = source_id or database_config["default_source"]
    source = database_config["sources"].get(resolved_source_id)
    if source is None:
        raise KeyError(f"Unknown database source: {resolved_source_id}")
    return copy.deepcopy(source)


DB_CONFIG = get_database_source_config()
DB_RUNTIME_CONFIG = APP_CONFIG["database"]["runtime"]
LLM_CONFIG = APP_CONFIG["llm"]
