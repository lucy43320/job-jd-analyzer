"""多用户：Supabase 认证 + 每人一份数据。

为什么是 Supabase 而不是别的：
  · WorkBuddy 自带的云服务只提供 JS SDK（给网页 / 小程序用），Streamlit 是 Python
    服务端应用，接不上；
  · Supabase 提供标准 REST（GoTrue 做认证、PostgREST 做数据），用 requests 就能调，
    不必再装一堆 SDK，云端装包也不会因为版本冲突失败。

数据隔离靠数据库的行级权限（RLS），不靠程序自觉：
  user_data 表每行带 user_id，策略写死 auth.uid() = user_id，
  就算有人拿到 anon key 直接调接口，也只能看到自己那一行。

配置（写进 .streamlit/secrets.toml 或环境变量，不写进代码）：
  SUPABASE_URL        https://xxxx.supabase.co
  SUPABASE_ANON_KEY   公开可见的 anon key（受 RLS 约束，不是管理员密钥）

没配这两项时 config() 返回 None，应用退回原来的单用户本地文件模式 —— 不装 Supabase
也能照常在本机用，这是刻意的：改造不能把现有用法弄坏。
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse

import requests

BUILD = 20261107161500

TIMEOUT = 20
# access_token 提前 60 秒算过期，避免"刚好卡在边界上"被服务端拒
EXPIRY_MARGIN = 60


class CloudError(Exception):
    """云端操作的失败。message 给用户看，不需要再套一层。"""


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def _read(name: str) -> str:
    """先读 st.secrets，再读环境变量。两处都没有就返回空串。"""
    try:
        import streamlit as st

        value = st.secrets.get(name, "")
        if value:
            return str(value).strip()
    except Exception:  # noqa: BLE001 —— 没有 secrets.toml 时 st.secrets 会抛异常
        pass
    return str(os.environ.get(name, "")).strip()


def config() -> dict | None:
    """云端配置。没配齐返回 None，调用方据此退回本地模式。"""
    url = _read("SUPABASE_URL")
    key = _read("SUPABASE_ANON_KEY")
    if not (url and key):
        return None
    return {"url": url.rstrip("/"), "key": key}


def enabled() -> bool:
    return config() is not None


# --------------------------------------------------------------------------
# 底层请求
# --------------------------------------------------------------------------


def _headers(cfg: dict, token: str | None = None, extra: dict | None = None) -> dict:
    head = {"apikey": cfg["key"], "Content-Type": "application/json"}
    if token:
        head["Authorization"] = f"Bearer {token}"
    if extra:
        head.update(extra)
    return head


def _post(url: str, cfg: dict, body: dict, token: str | None = None,
          extra: dict | None = None) -> requests.Response:
    try:
        return requests.post(
            url, headers=_headers(cfg, token, extra), data=json.dumps(body),
            timeout=TIMEOUT, proxies={"http": None, "https": None},
        )
    except requests.RequestException as exc:
        raise CloudError(f"连不上服务器（{type(exc).__name__}）。检查网络后再试。") from exc


def _get(url: str, cfg: dict, token: str | None = None) -> requests.Response:
    try:
        return requests.get(
            url, headers=_headers(cfg, token), timeout=TIMEOUT,
            proxies={"http": None, "https": None},
        )
    except requests.RequestException as exc:
        raise CloudError(f"连不上服务器（{type(exc).__name__}）。检查网络后再试。") from exc


def _error_text(resp: requests.Response) -> str:
    """把 Supabase 的报错翻成人话。原样抛英文对用户没用。"""
    try:
        payload = resp.json()
    except ValueError:
        return f"服务器返回 {resp.status_code}"

    code = str(payload.get("error_code") or payload.get("code") or "")
    msg = str(payload.get("msg") or payload.get("message") or payload.get("error_description") or "")
    mapping = {
        "invalid_credentials": "邮箱或密码不对。",
        "email_not_confirmed": "这个邮箱还没验证。去邮箱点确认链接，或在 Supabase 后台关掉邮箱验证。",
        "user_already_exists": "这个邮箱已经注册过了，直接登录吧。",
        "email_exists": "这个邮箱已经注册过了，直接登录吧。",
        "weak_password": "密码太短，至少 6 位。",
        "over_email_send_rate_limit": "验证邮件发得太频繁，等几分钟再试。",
        "signup_disabled": "这个项目关闭了注册，只能在 Supabase 后台加用户。",
    }
    for key, text in mapping.items():
        if key in code or key in msg:
            return text
    return msg or f"服务器返回 {resp.status_code}"


# --------------------------------------------------------------------------
# 认证
# --------------------------------------------------------------------------


def _session_from(resp: requests.Response) -> dict:
    payload = resp.json()
    user = payload.get("user") or payload
    return {
        "user_id": str(user.get("id") or ""),
        "email": str(user.get("email") or ""),
        "access_token": str(payload.get("access_token") or ""),
        "refresh_token": str(payload.get("refresh_token") or ""),
        "expires_at": time.time() + int(payload.get("expires_in") or 3600),
    }


def sign_up(cfg: dict, email: str, password: str) -> tuple[dict | None, str]:
    """注册。返回 (会话, 提示)。

    项目开了「邮箱验证」时，注册成功后拿不到 access_token —— 这时会话为空，
    提示用户去收邮件。是否自动登录取决于 Supabase 后台设置，这里如实反映。
    """
    resp = _post(f'{cfg["url"]}/auth/v1/signup', cfg, {"email": email, "password": password})
    if resp.status_code not in (200, 201):
        return None, _error_text(resp)
    session = _session_from(resp)
    if not session["access_token"]:
        return None, "注册成功。去邮箱点确认链接，然后回来登录。"
    return session, ""


def sign_in(cfg: dict, email: str, password: str) -> tuple[dict | None, str]:
    resp = _post(
        f'{cfg["url"]}/auth/v1/token?grant_type=password', cfg,
        {"email": email, "password": password},
    )
    if resp.status_code != 200:
        return None, _error_text(resp)
    return _session_from(resp), ""


def refresh(cfg: dict, session: dict) -> dict | None:
    """token 快过期时换一张。失败返回 None，由调用方把用户踢回登录页。"""
    token = session.get("refresh_token")
    if not token:
        return None
    resp = _post(
        f'{cfg["url"]}/auth/v1/token?grant_type=refresh_token', cfg,
        {"refresh_token": token},
    )
    if resp.status_code != 200:
        return None
    fresh = _session_from(resp)
    # refresh 的响应不一定带 user，保住原来的身份信息
    fresh["user_id"] = fresh["user_id"] or session.get("user_id", "")
    fresh["email"] = fresh["email"] or session.get("email", "")
    return fresh


def still_valid(session: dict) -> bool:
    return bool(session.get("access_token")) and (
        float(session.get("expires_at") or 0) - EXPIRY_MARGIN > time.time()
    )


# --------------------------------------------------------------------------
# 每人一份数据
# --------------------------------------------------------------------------


def fetch_data(cfg: dict, session: dict) -> dict | None:
    """读当前用户的数据。没有记录返回 None（第一次登录就是这种情况）。"""
    uid = urllib.parse.quote(session["user_id"], safe="")
    url = f'{cfg["url"]}/rest/v1/user_data?user_id=eq.{uid}&select=payload'
    resp = _get(url, cfg, session["access_token"])
    if resp.status_code != 200:
        raise CloudError(_error_text(resp))
    rows = resp.json()
    if not isinstance(rows, list) or not rows:
        return None
    payload = rows[0].get("payload")
    return payload if isinstance(payload, dict) else None


def save_data(cfg: dict, session: dict, data: dict) -> tuple[bool, str]:
    """整份数据 upsert 到当前用户名下。返回 (成功?, 提示)。"""
    body = {
        "user_id": session["user_id"],
        "payload": data,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    resp = _post(
        f'{cfg["url"]}/rest/v1/user_data', cfg, body, session["access_token"],
        extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    if resp.status_code in (200, 201, 204):
        return True, ""
    return False, _error_text(resp)


SETUP_SQL = """-- 在 Supabase 后台的 SQL Editor 里，整段粘贴执行一次
create table if not exists public.user_data (
  user_id uuid primary key references auth.users(id) on delete cascade,
  payload jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.user_data enable row level security;

-- 每人只能读写自己那一行。这三条策略是数据隔离的全部依据，别删。
drop policy if exists "own row read"   on public.user_data;
drop policy if exists "own row insert" on public.user_data;
drop policy if exists "own row update" on public.user_data;

create policy "own row read"   on public.user_data for select using (auth.uid() = user_id);
create policy "own row insert" on public.user_data for insert with check (auth.uid() = user_id);
create policy "own row update" on public.user_data for update using (auth.uid() = user_id);
"""
