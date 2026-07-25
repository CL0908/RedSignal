"""Supabase Auth 接入：校验前端带来的 JWT，得出可信的 user_id。

为什么后端必须自己验签，而不是信前端传的 user_id——
    改到这一版之前，`/ws/user/{user_id}` 直接把 URL 里的 id 当身份，
    任何人把地址栏改成别人的 id 就能收到对方的匹配提醒、社交卡和聊天。
    有了验签，user_id 只能来自 JWT 的 `sub`，前端说什么都不算数。

三种验签方式，按成本从低到高自动选择：
  1. 对称 HS256：配了 SUPABASE_JWT_SECRET 就本地验，零网络开销（最快，推荐）
  2. 非对称 ES256/RS256：新项目用轮换密钥，公钥挂在 JWKS 端点，本地验，无需配密钥
  3. 远端兜底：两者都没有时，拿 token 去问 /auth/v1/user。只需 anon key，
     少配一个密钥，代价是每次验证一个网络往返——所以结果按 token 缓存到其过期为止。

演示模式（REDSIGNAL_DEMO_MODE=1，默认开）：
    允许 mock_data 里的预置用户（u_demo_a / d01…）不带 token 直接连，
    否则「舞池密集场景」那套演示没数据可跑。上线时置 0 关掉。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx
import jwt
from jwt import PyJWKClient

log = logging.getLogger("redsignal.auth")


def _load_env() -> dict:
    """os.environ 优先，其次 redsignal/.env（与 persistence.py 同款）。"""
    env = dict(os.environ)
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    return env


class AuthError(Exception):
    """token 缺失、过期或签名不对。"""


class Auth:
    def __init__(self) -> None:
        env = _load_env()
        self.url = env.get("SUPABASE_URL", "").rstrip("/")
        self.secret = env.get("SUPABASE_JWT_SECRET", "")
        self.anon_key = env.get("SUPABASE_ANON_KEY", "")
        self.demo_mode = env.get("REDSIGNAL_DEMO_MODE", "1") not in ("0", "false", "False")
        self._jwk_client: Optional[PyJWKClient] = None
        self._remote_cache: dict[str, tuple[dict, float]] = {}   # token -> (claims, 过期时刻)
        self.enabled = bool(self.url)
        if not self.enabled:
            log.warning("auth 未启用（缺 SUPABASE_URL）——所有请求按演示模式放行")
        elif self.secret:
            log.info("验签方式：本地 HS256（SUPABASE_JWT_SECRET）")
        elif self.anon_key:
            log.info("验签方式：JWKS 本地验，失败则回落到 /auth/v1/user 远端校验。"
                     "配上 SUPABASE_JWT_SECRET 可省掉网络往返")
        else:
            log.warning("既无 SUPABASE_JWT_SECRET 也无 SUPABASE_ANON_KEY，无法验签")
        if self.demo_mode:
            log.warning("演示模式开启：预置 mock 用户可不带 token 连接。"
                        "上线务必设 REDSIGNAL_DEMO_MODE=0")

    @property
    def jwks(self) -> PyJWKClient:
        if self._jwk_client is None:
            self._jwk_client = PyJWKClient(
                f"{self.url}/auth/v1/.well-known/jwks.json", cache_keys=True)
        return self._jwk_client

    def _verify_remote(self, token: str) -> dict:
        """拿 token 去问 Supabase：/auth/v1/user 只对有效 token 返回 200。

        没有 JWT secret 时的兜底。结果缓存到 token 自身过期为止，
        避免每个请求一个网络往返。
        """
        if not self.anon_key:
            raise AuthError("no way to verify token (缺 SUPABASE_JWT_SECRET 与 SUPABASE_ANON_KEY)")
        hit = self._remote_cache.get(token)
        if hit and hit[1] > time.time():
            return hit[0]
        try:
            r = httpx.get(f"{self.url}/auth/v1/user", timeout=8.0, headers={
                "apikey": self.anon_key,
                "Authorization": f"Bearer {token}",
            })
        except Exception as e:
            raise AuthError(f"auth server unreachable: {e}") from e
        if r.status_code != 200:
            raise AuthError(f"rejected by auth server ({r.status_code})")
        user = r.json()
        claims = {"sub": user.get("id"), "email": user.get("email", "")}
        # 缓存到 token 的 exp（读 payload 但不信任它——签名已由服务端确认过了）
        try:
            exp = jwt.decode(token, options={"verify_signature": False}).get("exp", 0)
        except Exception:
            exp = 0
        self._remote_cache[token] = (claims, min(exp or 0, time.time() + 300))
        if len(self._remote_cache) > 1000:          # 防无上限增长
            self._remote_cache.clear()
        return claims

    def verify(self, token: str) -> dict:
        """校验 token，返回 claims。任何问题一律抛 AuthError，绝不放行。"""
        if not token:
            raise AuthError("missing token")
        token = token.removeprefix("Bearer ").strip()
        opts = {"verify_aud": False}      # Supabase 的 aud 是 "authenticated"，不做额外约束
        if self.secret:
            try:
                return jwt.decode(token, self.secret, algorithms=["HS256"], options=opts)
            except jwt.ExpiredSignatureError as e:
                raise AuthError("token expired") from e
            except Exception as e:
                raise AuthError(f"invalid token: {e}") from e
        # 没配 secret：先试 JWKS（非对称项目），不行再问服务端
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            return jwt.decode(token, key, algorithms=["ES256", "RS256"], options=opts)
        except jwt.ExpiredSignatureError as e:
            raise AuthError("token expired") from e
        except Exception:
            return self._verify_remote(token)

    def user_id_from_token(self, token: str) -> str:
        claims = self.verify(token)
        sub = claims.get("sub")
        if not sub:
            raise AuthError("token has no sub")
        return str(sub)

    def email_from_token(self, token: str) -> str:
        return str(self.verify(token).get("email", ""))

    def display_name_from_token(self, token: str) -> str:
        """注册时填的用户名。存在 Supabase 的 user_metadata 里，随 JWT 一起下发，
        所以不需要为它加数据库列——profiles 表本来就有 nickname。"""
        claims = self.verify(token)
        meta = claims.get("user_metadata") or {}
        for key in ("display_name", "name", "nickname", "username"):
            v = meta.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()[:24]
        return ""

    # ------------------------------------------------------------------
    def resolve(self, token: str, claimed_user_id: str = "") -> str:
        """把「前端声称的身份」换成「可信的身份」。

        有 token 一律以 token 为准。没有 token 时，只有演示模式下的预置用户放行。
        """
        if token:
            return self.user_id_from_token(token)
        if self.demo_mode and is_demo_user(claimed_user_id):
            return claimed_user_id
        raise AuthError("authentication required")


def is_demo_user(user_id: str) -> bool:
    """预置演示用户名单以外的 id，绝不允许无 token 冒充。"""
    from .mock_data import DEMO_USERS
    return user_id in DEMO_USERS


auth = Auth()
