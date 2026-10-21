# -*- coding: utf-8 -*-
"""生成 ChatBI API 访问令牌（HMAC-SHA256 签名）。

用法：
    export AUTH_SECRET="你的签名密钥"

    # admin 全量权限
    uv run python scripts/make_token.py --user-id u_zhang --role admin

    # ops（运营）角色：必须带 --operator-name（dim_stores.owner，如 运营A）
    uv run python scripts/make_token.py --user-id u_ops_a --role ops --operator-name 运营A

    # 自定义有效期（秒，默认 86400 一天）
    uv run python scripts/make_token.py --user-id u_zhang --role admin --ttl 3600

    # 直接指定密钥（不推荐，避免进入 shell 历史）
    uv run python scripts/make_token.py --user-id u1 --role admin --secret "xxx"

生成的令牌用于请求头：
    Authorization: Bearer <token>
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from security import make_auth_token  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 ChatBI API 访问令牌")
    parser.add_argument("--user-id", required=True, help="用户标识，写入审计日志")
    parser.add_argument("--role", required=True, help="角色：admin / ops（其余角色暂未配置）")
    parser.add_argument("--operator-name", default=None, help="ops 角色必填：运营负责人（dim_stores.owner）")
    parser.add_argument("--ttl", type=int, default=86400, help="有效期秒数，默认 86400")
    parser.add_argument("--secret", default=None, help="签名密钥，默认读环境变量 AUTH_SECRET")
    args = parser.parse_args()

    if args.role.lower() == "ops" and not args.operator_name:
        parser.error("ops 角色必须提供 --operator-name（如 运营A）")

    secret = args.secret or os.getenv("AUTH_SECRET")
    if not secret:
        parser.error("缺少签名密钥：请设置环境变量 AUTH_SECRET 或传 --secret")

    token = make_auth_token(
        user_id=args.user_id,
        role=args.role,
        secret=secret,
        expires_in=args.ttl,
        operator_name=args.operator_name,
    )
    print(token)
    print(
        f"\n请求头示例：\n  Authorization: Bearer {token}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
