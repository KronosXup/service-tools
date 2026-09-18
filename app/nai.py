"""NovelAI 上游客户端：令牌池、429 退避、SSE 透传。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx

from .policy import mask_token


class UpstreamError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class TokenState:
    __slots__ = (
        "token", "token_id", "v5_daily_limit", "allow_anlas", "pending_v5",
        "fails", "blocked_until", "disabled", "last_ok", "image_next_at",
    )

    def __init__(self, token: str, index: int, v5_daily_limit: int,
                 allow_anlas: bool):
        self.token = token
        # 永不把原始上游 Token 写入数据库；只存不可逆的短哈希标识。
        self.token_id = f"token-{index + 1}-" + hashlib.sha256(token.encode()).hexdigest()[:16]
        self.v5_daily_limit = max(0, v5_daily_limit)
        self.allow_anlas = allow_anlas
        self.pending_v5 = 0
        self.fails = 0
        self.blocked_until = 0.0
        self.disabled = False
        self.last_ok = 0.0
        self.image_next_at = 0.0

    @property
    def usable(self) -> bool:
        return not self.disabled and time.time() >= self.blocked_until


class NaiClient:
    """持有共享 httpx.AsyncClient 与上游令牌池。"""

    def __init__(self, tokens: list[str], image_host: str, text_host: str,
                 legacy_text_host: str, *, db: Any, day_fn: Callable[[], str],
                 v5_daily_limits: list[int], allow_anlas: list[bool],
                 image_min_interval: float = 15):
        self.image_host = image_host.rstrip("/")
        self.text_host = text_host.rstrip("/")
        self.legacy_text_host = legacy_text_host.rstrip("/")
        self.pool = [
            TokenState(
                token,
                index,
                v5_daily_limits[index] if index < len(v5_daily_limits) else 0,
                allow_anlas[index] if index < len(allow_anlas) else True,
            )
            for index, token in enumerate(tokens)
        ]
        self._db = db
        self._day_fn = day_fn
        self._image_min_interval = max(0.0, image_min_interval)
        self._rr = 0
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=15, read=300, write=120, pool=300),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
            headers={"User-Agent": "nai-gate/1.0"},
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    # ---------------- token pool ----------------
    async def pick_token(self, *, requires_anlas: bool = False,
                         v5_free: bool = False) -> Optional[TokenState]:
        """选取符合该图片费用策略的令牌；V5 限额在这里原子预留。"""
        async with self._lock:
            usable = [t for t in self.pool if t.usable and
                      (not requires_anlas or t.allow_anlas)]
            if not usable:
                return None

            # 保持轮询，同时跳过当日 V5 已用完的特定上游 Token。
            start = (self._rr + 1) % len(usable)
            chosen: Optional[TokenState] = None
            for offset in range(len(usable)):
                candidate = usable[(start + offset) % len(usable)]
                if v5_free and candidate.v5_daily_limit:
                    used = await self._db.get_upstream_v5_counter(
                        candidate.token_id, self._day_fn()
                    )
                    if used + candidate.pending_v5 >= candidate.v5_daily_limit:
                        continue
                chosen = candidate
                break
            if chosen is None:
                return None

            self._rr = usable.index(chosen)
            if v5_free and chosen.v5_daily_limit:
                chosen.pending_v5 += 1
            return chosen

    async def finish_v5_reservation(self, ts: TokenState, *, succeeded: bool,
                                    v5_free: bool) -> None:
        """只把成功完成的免费 V5 图计入特定上游令牌的日额度。"""
        if not v5_free or not ts.v5_daily_limit:
            return
        async with self._lock:
            ts.pending_v5 = max(0, ts.pending_v5 - 1)
            if succeeded:
                await self._db.bump_upstream_v5_counter(ts.token_id, self._day_fn())

    async def record_successful_images(self, ts: TokenState, image_count: int) -> None:
        """按上游实际成功响应记录生成张数；失败、拒绝和限流不计入。"""
        if image_count > 0:
            await self._db.bump_upstream_image_counter(
                ts.token_id, self._day_fn(), image_count
            )

    async def wait_for_token_image_slot(self, ts: TokenState) -> None:
        """每把上游 Token 各自保持图片请求间隔，不与其他 Token 共享计时。"""
        if not self._image_min_interval:
            return
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, ts.image_next_at - now)
            ts.image_next_at = max(now, ts.image_next_at) + self._image_min_interval
        if wait:
            await asyncio.sleep(wait)

    def mark_rate_limited(self, ts: TokenState, retry_after: float = 20.0) -> None:
        ts.blocked_until = time.time() + max(5.0, retry_after)
        ts.fails += 1

    def mark_unauthorized(self, ts: TokenState) -> None:
        ts.disabled = True

    def mark_ok(self, ts: TokenState) -> None:
        ts.fails = 0
        ts.last_ok = time.time()

    @property
    def configured(self) -> bool:
        return bool(self.pool)

    async def status(self) -> list[dict[str, Any]]:
        now = time.time()
        result = []
        for t in self.pool:
            counter = await self._db.get_upstream_counter(t.token_id, self._day_fn())
            result.append({
                "token": mask_token(t.token),
                "usable": t.usable,
                "disabled": t.disabled,
                "fails": t.fails,
                "blocked_for": max(0, int(t.blocked_until - now)),
                "last_ok": t.last_ok,
                "allow_anlas": t.allow_anlas,
                "v5_daily_limit": t.v5_daily_limit,
                "v5_used": counter["v5"],
                "images_today": counter["images"],
            })
        return result

    # ---------------- requests ----------------
    def _headers(self, ts: TokenState, accept: str = "*/*") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {ts.token}",
            "Accept": accept,
            "Content-Type": "application/json",
        }

    async def request(
        self, method: str, url: str, json_body: Any = None,
        accept: str = "*/*",
        on_rate_limited: Optional[Callable[[float], Awaitable[None]]] = None,
        *, requires_anlas: bool = False, v5_free: bool = False,
        image_count: int = 0, image_lane: bool = False,
    ) -> httpx.Response:
        """普通请求；对上游 429 做一次换 token 重试。"""
        if self._client is None:
            raise RuntimeError("client not started")
        attempts = 0
        last_resp: Optional[httpx.Response] = None
        while attempts < 2:
            attempts += 1
            ts = await self.pick_token(requires_anlas=requires_anlas, v5_free=v5_free)
            if ts is None:
                if requires_anlas:
                    raise UpstreamError(503, "没有允许使用 Anlas 的上游令牌，无法生成此图片")
                if v5_free:
                    raise UpstreamError(429, "可用上游令牌的今日 V5 免费图片额度已用完")
                raise UpstreamError(503, "上游令牌全部被限流或不可用，请稍后再试")
            try:
                if image_lane:
                    await self.wait_for_token_image_slot(ts)
                resp = await self._client.request(
                    method, url, json=json_body, headers=self._headers(ts, accept)
                )
            except Exception:
                await self.finish_v5_reservation(ts, succeeded=False, v5_free=v5_free)
                raise
            if resp.status_code == 429:
                await self.finish_v5_reservation(ts, succeeded=False, v5_free=v5_free)
                ra = 20.0
                try:
                    ra = float(resp.headers.get("retry-after", "20"))
                except ValueError:
                    pass
                self.mark_rate_limited(ts, ra)
                if on_rate_limited:
                    await on_rate_limited(max(5.0, ra))
                    if image_lane:
                        raise UpstreamError(429, "上游限流(429)，全站图片生成已进入冷却")
                last_resp = resp
                continue
            if resp.status_code == 401:
                await self.finish_v5_reservation(ts, succeeded=False, v5_free=v5_free)
                self.mark_unauthorized(ts)
                raise UpstreamError(502, "上游令牌已失效（401），请站长更换 NovelAI Token")
            await self.finish_v5_reservation(
                ts, succeeded=resp.status_code == 200, v5_free=v5_free
            )
            if resp.status_code == 200:
                await self.record_successful_images(ts, image_count)
            self.mark_ok(ts)
            return resp
        raise UpstreamError(429, "上游限流(429)，请降低频率后重试")

    async def stream(
        self, url: str, json_body: Any,
    ) -> AsyncIterator[httpx.Response]:
        """流式请求；只 Yield 一个 response，由调用方迭代字节。带一次换 token 重试。"""
        if self._client is None:
            raise RuntimeError("client not started")
        ts = await self.pick_token()
        if ts is None:
            raise UpstreamError(503, "上游令牌全部被限流或不可用，请稍后再试")
        req = self._client.build_request(
            "POST", url, json=json_body, headers=self._headers(ts, "text/event-stream")
        )
        resp = await self._client.send(req, stream=True)
        if resp.status_code in (401, 429, 502, 503):
            body = (await resp.aread()).decode("utf-8", "replace")[:300]
            await resp.aclose()
            if resp.status_code == 401:
                self.mark_unauthorized(ts)
                raise UpstreamError(502, "上游令牌已失效（401），请站长更换 NovelAI Token")
            if resp.status_code == 429:
                self.mark_rate_limited(ts)
                raise UpstreamError(429, "上游限流(429)，请降低频率后重试")
            raise UpstreamError(502, f"上游错误 {resp.status_code}: {body}")
        self.mark_ok(ts)
        return resp
