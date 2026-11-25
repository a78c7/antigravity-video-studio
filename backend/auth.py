"""
账号密码鉴权、签名 Session Cookie、同步 Token 校验与角色权限管理（纯 Python 标准库实现）：
- 密码哈希：hashlib.pbkdf2_hmac('sha256', ..., 200000) + 16 字节随机 salt；
- 会话令牌：hmac.new(SHA-256) + base64url，默认 7 天有效，HttpOnly + SameSite=Lax；
- 角色模型：admin（管理员/主创导演）与 partner（团队协作成员）；
- 支持通过环境变量 INIT_ADMIN_USER/PASS、INIT_PARTNER_USER/PASS 启动时幂等初始化，
  并提供 `python -m backend.auth` CLI 助手供自托管部署安全建号（严禁打印明文密码）。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import sys
import time
from contextvars import ContextVar, Token
from typing import Any

from fastapi import HTTPException, Request

from .config import DATA_DIR, is_cloud_collab, load_config
from .db import db_ctx, now_str

SESSION_COOKIE_NAME = "agvs_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
PBKDF2_ITERATIONS = 200_000
VALID_ROLES = ("admin", "partner")

# 本地制片模式默认免登录操作者身份
LOCAL_DEFAULT_ACTOR: dict[str, Any] = {
    "id": 0,
    "username": "local_director",
    "display_name": "本地导演",
    "role": "admin",
}

_current_actor_var: ContextVar[dict[str, Any] | None] = ContextVar("agvs_current_actor", default=None)
_FALLBACK_SECRET_CACHE: bytes | None = None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("ascii"))


def hash_password(password: str, salt: bytes | None = None) -> str:
    """使用 PBKDF2-HMAC-SHA256（200,000 次迭代 + 随机盐）生成密码摘要"""
    if not isinstance(password, str) or not password:
        raise ValueError("密码不能为空")
    use_salt = salt if salt is not None else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), use_salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${use_salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """恒定时间校验明文密码与已存 PBKDF2 哈希是否匹配"""
    if not password or not stored_hash:
        return False
    try:
        algo, iter_s, salt_hex, hash_hex = stored_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iter_s)
        if iterations < 10_000:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def _get_session_secret() -> bytes:
    """获取用于签发 Session Cookie 的服务端密钥（绝不落代码库）"""
    global _FALLBACK_SECRET_CACHE
    cfg_secret = (load_config().get("SESSION_SECRET") or "").strip()
    if cfg_secret:
        return cfg_secret.encode("utf-8")

    # 若未配置 SESSION_SECRET，则在受控 DATA_DIR 持久化随机实例密钥（权限 600）
    if _FALLBACK_SECRET_CACHE is not None:
        return _FALLBACK_SECRET_CACHE

    secret_file = DATA_DIR / ".session_secret"
    try:
        if secret_file.exists():
            raw = secret_file.read_text(encoding="utf-8").strip()
            if len(raw) >= 32:
                _FALLBACK_SECRET_CACHE = raw.encode("utf-8")
                return _FALLBACK_SECRET_CACHE
        generated = secrets.token_urlsafe(48)
        secret_file.write_text(generated, encoding="utf-8")
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
        _FALLBACK_SECRET_CACHE = generated.encode("utf-8")
        return _FALLBACK_SECRET_CACHE
    except OSError:
        _FALLBACK_SECRET_CACHE = secrets.token_bytes(32)
        return _FALLBACK_SECRET_CACHE


def create_session_token(user: dict[str, Any], ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """签发带过期时间的 HMAC-SHA256 会话令牌"""
    payload = {
        "uid": int(user.get("id") or 0),
        "u": str(user.get("username") or ""),
        "n": str(user.get("display_name") or user.get("username") or ""),
        "r": str(user.get("role") or "partner"),
        "exp": int(time.time()) + max(60, int(ttl_seconds)),
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(_get_session_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def verify_session_token(token: str | None) -> dict[str, Any] | None:
    """校验签名 Cookie 令牌并核实用户在数据库中处于激活状态"""
    if not token or "." not in token:
        return None
    parts = token.strip().split(".", 1)
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    if not payload_b64 or not sig_b64:
        return None
    try:
        expected_sig = hmac.new(_get_session_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
        provided_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, provided_sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if int(payload.get("exp") or 0) <= int(time.time()):
            return None
        uid = int(payload.get("uid") or 0)
        username = str(payload.get("u") or "").strip()
        if not username:
            return None
    except Exception:
        return None

    with db_ctx() as conn:
        row = conn.execute(
            "SELECT id, username, display_name, role, is_active FROM users WHERE id = ? AND username = ?",
            (uid, username),
        ).fetchone()
        if not row or not int(row["is_active"] or 0):
            return None
        return {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"] or row["username"]),
            "role": str(row["role"] or "partner"),
        }


def verify_sync_token(provided_token: str | None) -> bool:
    """使用恒定时间比对校验本地↔远程协作同步请求头 X-Sync-Token"""
    expected = (load_config().get("SYNC_API_TOKEN") or "").strip()
    candidate = (provided_token or "").strip()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


def set_request_actor(actor: dict[str, Any] | None) -> Token[dict[str, Any] | None]:
    """将当前请求的已鉴权操作者绑定到上下文变量（供同步/异步路由共用）"""
    return _current_actor_var.set(actor)


def reset_request_actor(token: Token[dict[str, Any] | None]) -> None:
    _current_actor_var.reset(token)


def get_current_actor(request: Request | None = None) -> dict[str, Any]:
    """获取当前操作者信息；local_producer 模式免登录默认返回本地导演 (admin)"""
    if request is not None:
        state_user = getattr(request.state, "user", None)
        if isinstance(state_user, dict) and state_user.get("username"):
            return state_user
    ctx_user = _current_actor_var.get()
    if isinstance(ctx_user, dict) and ctx_user.get("username"):
        return ctx_user
    if not is_cloud_collab():
        return dict(LOCAL_DEFAULT_ACTOR)
    return {"id": 0, "username": "anonymous", "display_name": "未登录用户", "role": "guest"}


def require_authenticated_user(request: Request) -> dict[str, Any]:
    """要求已登录用户（local_producer 模式自动回退为本地导演）"""
    state_user = getattr(request.state, "user", None)
    if isinstance(state_user, dict) and state_user.get("username"):
        return state_user
    if not is_cloud_collab():
        return dict(LOCAL_DEFAULT_ACTOR)
    raise HTTPException(status_code=401, detail="请先登录协作账号")


def require_admin_user(request: Request) -> dict[str, Any]:
    """要求管理员权限（role == 'admin'）"""
    user = require_authenticated_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return user


def is_https_request(request: Request) -> bool:
    """判断当前请求是否经 HTTPS（含反向代理 X-Forwarded-Proto）"""
    xfp = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if xfp:
        return xfp == "https"
    return request.url.scheme == "https"


def upsert_user(
    username: str,
    password: str,
    *,
    display_name: str = "",
    role: str = "partner",
    is_active: int = 1,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """幂等创建或更新账号（安全哈希存储，绝不返回 password_hash）"""
    uname = (username or "").strip()
    if not uname:
        raise ValueError("用户名不能为空")
    norm_role = role if role in VALID_ROLES else "partner"
    dname = (display_name or "").strip() or ("管理员" if norm_role == "admin" else uname)
    ts = now_str()

    def _run(c: sqlite3.Connection) -> dict[str, Any]:
        existing = c.execute(
            "SELECT id, username, display_name, role, password_hash, is_active FROM users WHERE username = ?",
            (uname,),
        ).fetchone()
        if existing is None:
            pw_hash = hash_password(password)
            cur = c.execute(
                """
                INSERT INTO users (username, display_name, role, password_hash, is_active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (uname, dname, norm_role, pw_hash, int(is_active), ts, ts),
            )
            uid = int(cur.lastrowid)
        else:
            uid = int(existing["id"])
            same_pw = verify_password(password, existing["password_hash"] or "")
            pw_hash = existing["password_hash"] if same_pw else hash_password(password)
            if (
                not same_pw
                or existing["display_name"] != dname
                or existing["role"] != norm_role
                or int(existing["is_active"] or 0) != int(is_active)
            ):
                c.execute(
                    """
                    UPDATE users
                    SET display_name = ?, role = ?, password_hash = ?, is_active = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (dname, norm_role, pw_hash, int(is_active), ts, uid),
                )
        row = c.execute(
            "SELECT id, username, display_name, role, is_active, created_at, updated_at FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
        return dict(row)

    if conn is not None:
        return _run(conn)
    with db_ctx() as managed:
        return _run(managed)


def authenticate_user(username: str, password: str) -> dict[str, Any] | None:
    """校验用户名与密码，成功返回脱敏用户字典（不含密码哈希）"""
    uname = (username or "").strip()
    if not uname or not password:
        return None
    with db_ctx() as conn:
        row = conn.execute(
            "SELECT id, username, display_name, role, password_hash, is_active FROM users WHERE username = ?",
            (uname,),
        ).fetchone()
        if not row or not int(row["is_active"] or 0):
            return None
        if not verify_password(password, row["password_hash"] or ""):
            return None
        return {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "display_name": str(row["display_name"] or row["username"]),
            "role": str(row["role"] or "partner"),
        }


def bootstrap_users_from_env(conn: sqlite3.Connection | None = None) -> list[str]:
    """根据环境变量 INIT_ADMIN_USER/PASS、INIT_PARTNER_USER/PASS 幂等初始化或同步账号"""
    created_or_synced: list[str] = []
    specs = [
        (
            os.environ.get("INIT_ADMIN_USER", "").strip(),
            os.environ.get("INIT_ADMIN_PASS", "").strip(),
            os.environ.get("INIT_ADMIN_NAME", "").strip() or "管理员",
            "admin",
        ),
        (
            os.environ.get("INIT_PARTNER_USER", "").strip(),
            os.environ.get("INIT_PARTNER_PASS", "").strip(),
            os.environ.get("INIT_PARTNER_NAME", "").strip() or "协作成员",
            "partner",
        ),
    ]
    for uname, pwd, dname, role in specs:
        if uname and pwd:
            upsert_user(uname, pwd, display_name=dname, role=role, is_active=1, conn=conn)
            created_or_synced.append(uname)
    return created_or_synced


def _cli_main(argv: list[str] | None = None) -> int:
    """供自托管部署脚本安全建号与排查的 CLI 入口（严禁输出任何明文密码或哈希）"""
    from .db import init_db

    parser = argparse.ArgumentParser(description="反重力视频工坊账号管理工具（零明文输出）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create-user", help="创建或更新协作账号")
    p_create.add_argument("--username", required=True, help="登录用户名")
    p_create.add_argument("--password", default="", help="登录密码（建议优先用 --password-env 或 stdin）")
    p_create.add_argument("--password-env", default="", help="从指定环境变量读取密码，避免命令行历史留痕")
    p_create.add_argument("--display-name", default="", help="页面显示昵称")
    p_create.add_argument("--role", choices=VALID_ROLES, default="partner", help="角色：admin 或 partner")

    sub.add_parser("bootstrap", help="按环境变量 INIT_ADMIN_*/INIT_PARTNER_* 幂等初始化账号")
    sub.add_parser("list-users", help="列出已有账号（脱敏，不含密码哈希）")

    args = parser.parse_args(argv)
    init_db()

    if args.cmd == "bootstrap":
        synced = bootstrap_users_from_env()
        print(json.dumps({"ok": True, "synced_users": synced}, ensure_ascii=False))
        return 0

    if args.cmd == "create-user":
        pwd = args.password
        if not pwd and args.password_env:
            pwd = os.environ.get(args.password_env, "").strip()
        if not pwd and not sys.stdin.isatty():
            pwd = sys.stdin.read().strip()
        if not pwd:
            print("错误：未提供密码（请传 --password-env、标准输入或 --password）", file=sys.stderr)
            return 2
        user = upsert_user(
            args.username,
            pwd,
            display_name=args.display_name,
            role=args.role,
            is_active=1,
        )
        print(json.dumps({"ok": True, "user": user}, ensure_ascii=False))
        return 0

    if args.cmd == "list-users":
        with db_ctx() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, username, display_name, role, is_active, created_at, updated_at FROM users ORDER BY id ASC"
                ).fetchall()
            ]
        print(json.dumps({"users": rows}, ensure_ascii=False))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())
