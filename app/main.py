"""NAI Gate —— NovelAI 公益分发网关。

别人拿到的是本站签发的虚拟 Key（nai-xxx），真实 NovelAI Token 只保存在服务端。
网关负责：鉴权 -> 限流(RPM/并发/排队) -> 配额检查 -> 参数钳制(防爆费) -> 透传 -> 记账。
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile

from . import admin
from .config import load_settings
from .nai import NaiClient, UpstreamError
from .policy import (
    clamp_image_params,
    clamp_text_params,
    estimate_image_cost,
    image_model_tier,
    estimate_tokens,
    gen_key,
    text_model_host,
)
from .state import GateState

SETTINGS = load_settings()
STATE: Optional[GateState] = None

DEFAULT_ANNOUNCEMENT = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>NAI Gate</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
     max-width:760px;margin:48px auto;padding:0 20px;color:#e6e6e6;background:#11171f;line-height:1.8}
h1{color:#7cc4ff;font-size:1.6em} code{background:#1d2733;padding:2px 8px;border-radius:6px}
a{color:#7cc4ff} .card{background:#161e29;border:1px solid #243043;border-radius:12px;padding:18px 22px;margin:18px 0}
</style></head><body>
<h1>NAI Gate · NovelAI 中转网关</h1>
<div class="card">
本站为 NovelAI 资源分发的中转服务。使用方法：<br>
1. 向站长申请一把虚拟 Key（形如 <code>nai-xxxxxxxx</code>）。<br>
2. 在支持自定义 NovelAI API 地址的客户端中，把 API 地址改为本站地址，Key 填虚拟 Key。<br>
3. 也提供 OpenAI 兼容接口 <code>/v1/chat/completions</code>（文本续写）。<br>
4. 站长入口：<a href="/admin">管理后台</a>。
</div>
<div class="card">站长可在后台「公告设置」中编辑本页面内容。</div>
</body></html>"""


class GateError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def err(status: int, message: str) -> GateError:
    return GateError(status, message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global STATE
    STATE = GateState(SETTINGS)
    await STATE.db.connect()
    await STATE.load_image_cooldown()
    removed_keys = await STATE.delete_inactive_keys()
    if removed_keys:
        print(f"[info] deleted {removed_keys} inactive API key(s)")
    await STATE.nai.start()
    if SETTINGS.seed_demo_key:
        if not await STATE.db.get_key_by_token("nai-demo-key"):
            row = await STATE.db.create_key({
                "name": "演示钥匙", "token": gen_key("nai"),
                "daily_images": SETTINGS.default_daily_images,
                "monthly_anlas": SETTINGS.default_monthly_anlas,
                "daily_text_tokens": SETTINGS.default_daily_text_tokens,
                "rpm": SETTINGS.default_rpm, "expires_at": None,
            })
            print(f"[seed] 演示 Key: {row['token']}")
    if not SETTINGS.admin_password:
        print("[warn] 未设置 ADMIN_PASSWORD，/admin 管理端将无法登录！")
    if not SETTINGS.nai_tokens:
        print("[warn] 未设置 NAI_TOKENS，所有生成请求将返回 503")
    app.state.gate = STATE
    cleanup_task = asyncio.create_task(inactive_key_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await STATE.nai.close()
        await STATE.db.close()


app = FastAPI(title="NAI Gate", docs_url=None, redoc_url=None, lifespan=lifespan)
app.include_router(admin.router)


@app.exception_handler(GateError)
async def gate_error_handler(request: Request, exc: GateError):
    return JSONResponse({"error": {"message": exc.message, "status": exc.status}},
                        status_code=exc.status)


@app.exception_handler(Exception)
async def fallback_handler(request: Request, exc: Exception):
    return JSONResponse({"error": {"message": f"服务器内部错误: {exc}", "status": 500}},
                        status_code=500)


# ================================================================ helpers ====

async def authenticate(request: Request):
    """校验虚拟 Key。"""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        raise err(401, "缺少 API Key")
    row = await STATE.db.get_key_by_token(token)
    if not row:
        raise err(401, "无效的 API Key")
    if not row["enabled"]:
        raise err(403, "该 Key 已被禁用")
    if row["expires_at"] and row["expires_at"] < time.time():
        raise err(403, "该 Key 已过期，请联系站长续期")
    # 只要 Key 实际通过鉴权即视为使用，避免 Launcher 登录/上游暂时失败时被误删。
    await STATE.db.touch_key(row["id"])
    return row


async def inactive_key_cleanup_loop() -> None:
    """常驻服务每小时回收一次长期闲置 Key。"""
    while True:
        try:
            removed = await STATE.delete_inactive_keys()
            if removed:
                print(f"[info] deleted {removed} inactive API key(s)")
        except Exception as exc:
            print(f"[warn] inactive key cleanup failed: {exc}")
        await asyncio.sleep(3600)


async def check_rpm(key) -> None:
    if key["is_admin"]:
        return
    ok = await STATE.hit_rpm(key["id"], key["rpm"])
    if not ok:
        raise err(429, f"请求过于频繁（上限 {key['rpm']} 次/分钟），请稍后再试")


def check_image_cooldown() -> None:
    remaining = STATE.image_cooldown_remaining()
    if remaining:
        raise err(429, f"上游图片服务限流保护中，所有图片生成暂停约 {remaining} 秒")


async def read_json(request: Request, limit_mb: float = 25) -> dict:
    cl = request.headers.get("content-length")
    if cl and float(cl) > limit_mb * 1024 * 1024:
        raise err(413, "请求体过大")
    try:
        data = await request.json()
    except Exception:
        raise err(400, "请求体不是合法 JSON")
    if not isinstance(data, dict):
        raise err(400, "请求体必须是 JSON 对象")
    return data


async def read_image_payload(request: Request, limit_mb: float = 25) -> dict:
    """兼容原生 NAI JSON 与 Launcher 的 multipart `request` JSON 部分。"""
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith("multipart/form-data"):
        return await read_json(request, limit_mb)

    cl = request.headers.get("content-length")
    if cl and float(cl) > limit_mb * 1024 * 1024:
        raise err(413, "请求体过大")
    try:
        form = await request.form(max_files=2, max_fields=10)
    except Exception:
        raise err(400, "multipart 请求格式无效或过大")
    part = form.get("request")
    if isinstance(part, UploadFile):
        try:
            raw = await part.read()
        finally:
            await part.close()
    elif isinstance(part, str):
        raw = part.encode("utf-8")
    else:
        raise err(400, "multipart 请求缺少 request JSON 字段")
    # 其余文件字段是 img2img/参考图载体；本站未开放时不能静默忽略。
    if any(name != "request" for name, _ in form.multi_items()):
        raise err(400, "本站未开放携带图片附件的 img2img / 参考图请求")
    if len(raw) > limit_mb * 1024 * 1024:
        raise err(413, "请求体过大")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise err(400, "multipart request 字段不是合法 JSON")
    if not isinstance(data, dict):
        raise err(400, "multipart request 字段必须是 JSON 对象")
    return data


@asynccontextmanager
async def acquire_concurrency(key):
    """全站 + 单 Key 两级并发闸门。仅获取锁有排队超时，持有后不超时。"""
    t = STATE.settings.queue_timeout
    if key["is_admin"]:
        try:
            async with asyncio.timeout(t):
                await STATE.global_sem.acquire()
        except (TimeoutError, asyncio.TimeoutError):
            raise err(429, "当前排队人数过多，请稍后再试")
        try:
            yield
        finally:
            STATE.global_sem.release()
        return
    ksem = STATE.key_sem(key["id"], STATE.settings.key_concurrency)
    try:
        async with asyncio.timeout(t):
            await ksem.acquire()
            try:
                await STATE.global_sem.acquire()
            except BaseException:
                ksem.release()
                raise
    except (TimeoutError, asyncio.TimeoutError):
        raise err(429, "当前排队人数过多，请稍后再试")
    try:
        yield
    finally:
        STATE.global_sem.release()
        ksem.release()


async def quota_image_check(key, est: dict) -> None:
    if key["is_admin"]:
        return
    c = await STATE.db.get_counter(key["id"], STATE.day())
    if est["v5"] > 0:
        # V5 周额度是账户级共享资源，用全站日计数镜像（恢复量 ~190 张/天）
        if key["daily_v5"] > 0 and c["v5"] + est["v5"] > key["daily_v5"]:
            raise err(429, f"已达今日 V5 额度（{key['daily_v5']} 张/天），明天恢复后再用")
        g = float(await STATE.db.get_setting(
            "global_daily_v5", STATE.settings.global_daily_v5) or 0)
        if g > 0 and not key["exclude_global_v5"]:
            total = await STATE.db.day_v5_total(STATE.day())
            if total + est["v5"] > g:
                raise err(402, f"全站今日 V5 额度已用完（{int(g)} 张/天），明天再来")
    if est["anlas"] > 0:
        if not key["allow_anlas"]:
            raise err(402, "该请求会消耗 Anlas，此 Key 未开通付费额度权限")
        if key["daily_anlas"] > 0 and c["anlas"] + est["anlas"] > key["daily_anlas"]:
            raise err(402, f"今日 Anlas 额度不足（已用 {c['anlas']:.0f} / 上限 "
                           f"{key['daily_anlas']:.0f}），明日恢复")
        used = await STATE.db.month_anlas(key["id"], STATE.month())
        if key["monthly_anlas"] > 0 and used + est["anlas"] > key["monthly_anlas"]:
            raise err(402, f"本月 Anlas 额度不足（已用 {used:.0f}/{key['monthly_anlas']:.0f}）")
        budget = float(await STATE.db.get_setting(
            "global_monthly_anlas", STATE.settings.global_monthly_anlas) or 0)
        if budget > 0:
            all_used = await STATE.db.month_anlas_all(STATE.month())
            if all_used + est["anlas"] > budget:
                raise err(402, f"全站本月 Anlas 预算已耗尽（{budget:.0f}），请联系站长")


def record(key, kind: str, model: str, status: str, *, images: int = 0,
           anlas: float = 0.0, tokens: int = 0, v5: int = 0, detail: str = "") -> None:
    """写日志；成功请求额外计入每日配额。"""
    async def _go():
        await STATE.db.add_log(key["id"], key["name"], kind, model, status,
                               images=images, anlas=anlas, tokens=tokens, detail=detail)
        if status == "ok":
            await STATE.db.bump_counters(
                key["id"], STATE.day(),
                images=images, anlas=anlas, text_tokens=tokens, requests=1, v5=v5,
            )
            await STATE.db.touch_key(key["id"])
    asyncio.create_task(_go())


async def upstream_call(url: str, payload: dict, accept: str = "*/*", *,
                        on_rate_limited=None, requires_anlas: bool = False,
                        v5_free: bool = False, image_count: int = 0,
                        image_lane: bool = False) -> httpx.Response:
    try:
        return await STATE.nai.request(
            "POST", url, payload, accept=accept, on_rate_limited=on_rate_limited,
            requires_anlas=requires_anlas, v5_free=v5_free, image_count=image_count,
            image_lane=image_lane,
        )
    except UpstreamError as e:
        raise err(e.status if e.status in (429, 503) else 502, e.message)


def _text_url(model: str, stream: bool = True) -> str:
    host = text_model_host(model, STATE.nai.text_host, STATE.nai.legacy_text_host)
    return f"{host}/ai/generate{'-stream' if stream else ''}"


async def _text_quota_check(key, payload: dict) -> None:
    if key["is_admin"]:
        return
    c = await STATE.db.get_counter(key["id"], STATE.day())
    if key["daily_text_tokens"] >= 0 and c["text_tokens"] >= key["daily_text_tokens"]:
        raise err(429, f"已达今日文本额度（{key['daily_text_tokens']} tokens/天），明日再来吧")


# ============================================================== 图片生成 =====

async def generate_image(request: Request):
    key = await authenticate(request)
    check_image_cooldown()
    await check_rpm(key)
    body = await read_image_payload(request)
    model = str(body.get("model", "?"))
    model_tier = image_model_tier(model)
    if model_tier is None:
        record(key, "image", model, "rejected", detail="未列入本站图片模型白名单")
        raise err(400, "不支持或尚未开放的图片模型")
    if model_tier == "v5" and not key["is_admin"] and key["image_model_scope"] != "all":
        record(key, "image", model, "rejected", detail="模型权限：仅允许 V4.5 及更低")
        raise err(403, "该 Key 仅允许 V4.5 及更低图片模型")

    # img2img 权限：Key 标记 + 全局开关 双重控制（无论是否钳制都先查）
    p0 = body.get("parameters", {}) or {}
    if (p0.get("image") or p0.get("mask")) and not (
            bool(key["is_admin"]) or
            (bool(key["allow_img2img"]) and STATE.settings.allow_img2img)):
        record(key, "image", model, "rejected", detail="img2img 未开放")
        raise err(400, "本站未开放 img2img / 局部重绘（该功能会消耗 Anlas）")

    # 免费档钳制：只对未开通 Anlas 的 Key 生效；开通 Anlas 的 Key 靠配额约束
    if STATE.settings.safe_clamp and not key["allow_anlas"]:
        body, notes, problem = clamp_image_params(
            body,
            max_pixels=STATE.settings.max_pixels,
            max_steps=STATE.settings.max_steps,
            allow_img2img=True,  # 权限已在上面预检
        )
        if problem:
            record(key, "image", model, "rejected", detail=problem)
            raise err(400, problem)
    else:
        notes = []

    p = body.get("parameters", {})
    try:
        image_count = int(p.get("n_samples", 1) or 1)
    except (TypeError, ValueError):
        raise err(400, "n_samples 必须是正整数")
    if image_count < 1:
        raise err(400, "n_samples 必须是正整数")

    est = estimate_image_cost(body, is_opus=True)
    await quota_image_check(key, est)

    cost = (f"est={est['anlas']}A" if est["anlas"]
            else (f"V5额度+{est['v5']}" if est["v5"] else "免费"))
    detail = "; ".join(notes) if notes else (
        f"{p.get('width')}x{p.get('height')}/{p.get('steps')}step {cost}")

    async def record_image_429(retry_after: float) -> None:
        cooldown = await STATE.block_image_generation(max(
            STATE.settings.image_429_cooldown_seconds, retry_after
        ))
        record(
            key, "image", model, "error",
            detail=(f"上游限流(429)：全站图片生成暂停约 {cooldown} 秒，"
                    "保护上游 Token"),
        )

    if not key["is_admin"]:
        await STATE.wait_for_key_image_slot(key["id"])
    async with acquire_concurrency(key):
        # 请求在队列中等待时可能刚触发全站冷却，放行前再检查一次。
        check_image_cooldown()
        resp = await upstream_call(
            f"{STATE.nai.image_host}/ai/generate-image", body,
            on_rate_limited=record_image_429,
            requires_anlas=est["anlas"] > 0,
            v5_free=est["v5"] > 0,
            image_count=image_count,
            image_lane=True,
        )
        if resp.status_code != 200:
            record(key, "image", model, "error",
                   detail=f"upstream {resp.status_code}: {resp.text[:200]}")
            return Response(resp.content, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "application/json"))
        record(key, "image", model, "ok", images=image_count, anlas=est["anlas"],
               v5=est["v5"], detail=detail)
        return Response(resp.content, status_code=200,
                        media_type=resp.headers.get("content-type", "application/octet-stream"))


async def suggest_tags(request: Request):
    key = await authenticate(request)
    check_image_cooldown()
    await check_rpm(key)
    if request.method == "GET":
        body = {
            "prompt": request.query_params.get("prompt", ""),
            "model": request.query_params.get("model", ""),
        }
    else:
        body = await read_json(request)

    async def record_tag_429(retry_after: float) -> None:
        cooldown = await STATE.block_image_generation(max(
            STATE.settings.image_429_cooldown_seconds, retry_after
        ))
        record(
            key, "tags", "", "error",
            detail=(f"上游限流(429)：全站图片生成暂停约 {cooldown} 秒，"
                    "保护上游 Token"),
        )

    if not key["is_admin"]:
        await STATE.wait_for_key_image_slot(key["id"])
    async with acquire_concurrency(key):
        check_image_cooldown()
        resp = await upstream_call(
            f"{STATE.nai.image_host}/ai/generate-image/suggest-tags", body,
            accept="application/json", on_rate_limited=record_tag_429, image_lane=True)
    if resp.status_code != 200:
        return Response(resp.content, status_code=resp.status_code, media_type="application/json")
    record(key, "tags", "", "ok")
    return Response(resp.content, media_type="application/json")


async def generate_image_stream_unavailable(request: Request):
    """明确告知 Launcher：本站只实现 ZIP 生图，不伪造 MessagePack 流。"""
    key = await authenticate(request)
    record(key, "image_stream", "", "rejected", detail="本站未开放 generate-image-stream")
    raise err(501, "本站暂未开放流式生图；请在启动器中关闭流式预览后重试")


# ============================================================== 文本生成 =====

async def generate_stream(request: Request):
    key = await authenticate(request)
    await check_rpm(key)
    body = await read_json(request)
    model = str(body.get("model", "?"))

    body, notes, problem = clamp_text_params(
        body,
        max_output_tokens=STATE.settings.max_text_output_tokens,
        max_input_chars=STATE.settings.max_input_chars,
    )
    if problem:
        record(key, "text", model, "rejected", detail=problem)
        raise err(400, problem)
    await _text_quota_check(key, body)

    daily_limit = -1 if key["is_admin"] else key["daily_text_tokens"]
    already = (await STATE.db.get_counter(key["id"], STATE.day()))["text_tokens"]

    async with acquire_concurrency(key):
        try:
            resp = await STATE.nai.stream(_text_url(model, True), body)
        except UpstreamError as e:
            record(key, "text", model, "error", detail=e.message)
            raise err(e.status if e.status in (429, 503) else 502, e.message)

        async def passthrough() -> AsyncIterator[bytes]:
            counted = 0
            buf = b""
            hard_cut = False
            try:
                async for chunk in resp.aiter_bytes():
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        s = line.strip()
                        if s.startswith(b"data:"):
                            data = s[5:].strip()
                            if data != b"[DONE]":
                                counted += 1
                                if 0 <= daily_limit <= already + counted:
                                    hard_cut = True
                                    yield b"data: [DONE]\n\n"
                                    return
                        yield line + b"\n"
                    if buf.endswith(b"\r"):
                        pass
            finally:
                await resp.aclose()
                d = ("达到每日上限被截断; " if hard_cut else "") + "; ".join(notes)
                record(key, "text", model, "ok", tokens=counted, detail=d.strip("; "))

        return StreamingResponse(passthrough(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})


async def generate_text(request: Request):
    key = await authenticate(request)
    await check_rpm(key)
    body = await read_json(request)
    model = str(body.get("model", "?"))

    body, notes, problem = clamp_text_params(
        body,
        max_output_tokens=STATE.settings.max_text_output_tokens,
        max_input_chars=STATE.settings.max_input_chars,
    )
    if problem:
        record(key, "text", model, "rejected", detail=problem)
        raise err(400, problem)
    await _text_quota_check(key, body)

    async with acquire_concurrency(key):
        resp = await upstream_call(_text_url(model, False), body, accept="application/json")
    if resp.status_code != 200:
        record(key, "text", model, "error", detail=f"upstream {resp.status_code}")
        return Response(resp.content, status_code=resp.status_code, media_type="application/json")
    try:
        out = (resp.json() or {}).get("output", "")
    except Exception:
        out = ""
    record(key, "text", model, "ok", tokens=estimate_tokens(out), detail="; ".join(notes))
    return Response(resp.content, media_type="application/json")


async def generate_voice(request: Request):
    key = await authenticate(request)
    await check_rpm(key)
    body = await read_json(request, limit_mb=1)
    async with acquire_concurrency(key):
        resp = await upstream_call(f"{STATE.nai.legacy_text_host}/ai/generate-voice", body)
    if resp.status_code != 200:
        return Response(resp.content, status_code=resp.status_code, media_type="application/json")
    record(key, "voice", str(body.get("voice", "")), "ok")
    return Response(resp.content, media_type=resp.headers.get("content-type", "audio/mpeg"))


# ====================================================== OpenAI 兼容桥(文本) ====

TEXT_MODELS = [
    "llama-3-erato-v1", "kayra-v1", "clio-v1",
    "nai-glm-4-6", "nai-xialong",
]


async def v1_models(request: Request):
    return {"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "novelai"} for m in TEXT_MODELS
    ]}


async def v1_me(request: Request):
    key = await authenticate(request)
    c = await STATE.db.get_counter(key["id"], STATE.day())
    return {
        "name": key["name"],
        "is_admin": bool(key["is_admin"]),
        "today": {
            "images": c["images"], "daily_images": 0,
            "anlas_today": round(float(c["anlas"]), 2),
            "daily_anlas": key["daily_anlas"],
            "v5_today": c["v5"], "daily_v5": key["daily_v5"],
            "image_model_scope": key["image_model_scope"],
            "anlas_month": round(await STATE.db.month_anlas(key["id"], STATE.month()), 2),
            "monthly_anlas": key["monthly_anlas"],
            "text_tokens": c["text_tokens"], "daily_text_tokens": key["daily_text_tokens"],
            "requests": c["requests"],
        },
        "expires_at": key["expires_at"],
    }


async def v1_chat(request: Request):
    key = await authenticate(request)
    await check_rpm(key)
    body = await read_json(request)
    want_stream = bool(body.get("stream"))

    msgs = body.get("messages") or []
    input_text = "\n".join(
        str(m.get("content", "")) for m in msgs if isinstance(m, dict)
    ).strip()
    model_in = str(body.get("model", ""))
    model = model_in if model_in in TEXT_MODELS else "llama-3-erato-v1"
    ml = int(body.get("max_tokens") or 150)
    payload = {
        "input": input_text,
        "model": model,
        "parameters": {
            "max_length": min(max(1, ml), STATE.settings.max_text_output_tokens),
            "min_length": 1,
            "temperature": float(body.get("temperature", 0.9) or 0.9),
            "top_p": float(body.get("top_p", 0.9) or 0.9),
            "top_k": 40,
        },
    }
    nai_body, _, problem = clamp_text_params(
        payload,
        max_output_tokens=STATE.settings.max_text_output_tokens,
        max_input_chars=STATE.settings.max_input_chars,
    )
    if problem:
        raise err(400, problem)
    await _text_quota_check(key, nai_body)

    daily_limit = -1 if key["is_admin"] else key["daily_text_tokens"]
    already = (await STATE.db.get_counter(key["id"], STATE.day()))["text_tokens"]

    async with acquire_concurrency(key):
        try:
            resp = await STATE.nai.stream(_text_url(model, True), nai_body)
        except UpstreamError as e:
            record(key, "chat", model, "error", detail=e.message)
            raise err(e.status if e.status in (429, 503) else 502, e.message)

        def openai_chunk(content: str, finish: Optional[str] = None) -> str:
            return json.dumps({
                "id": "chatcmpl-naigate", "object": "chat.completion.chunk",
                "created": int(time.time()), "model": model,
                "choices": [{"index": 0,
                             "delta": {"content": content} if content else {},
                             "finish_reason": finish}],
            }, ensure_ascii=False)

        result: dict[str, Any] = {"collected": [], "counted": 0}

        async def run_stream() -> AsyncIterator[bytes]:
            """消费 NAI SSE；流式时产出 OpenAI chunk，非流式时只收集文本。"""
            buf = b""
            try:
                if want_stream:
                    yield (openai_chunk("", None) + "\n\n").encode()
                done = False
                async for chunk in resp.aiter_bytes():
                    if done:
                        break
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        s = line.strip()
                        if not s.startswith(b"data:"):
                            continue
                        data = s[5:].strip()
                        if data == b"[DONE]":
                            done = True
                            break
                        text = ""
                        try:
                            obj = json.loads(data)
                            if isinstance(obj, dict):
                                text = str(obj.get("token", ""))
                        except Exception:
                            text = data.decode("utf-8", "replace")
                        if not text:
                            continue
                        result["counted"] += 1
                        if 0 <= daily_limit <= already + result["counted"]:
                            done = True
                            break
                        if want_stream:
                            yield (openai_chunk(text, None) + "\n\n").encode()
                        else:
                            result["collected"].append(text)
                if want_stream:
                    yield (openai_chunk("", "stop") + "\n\n").encode()
                    yield b"data: [DONE]\n\n"
            finally:
                await resp.aclose()
                record(key, "chat", model, "ok", tokens=result["counted"])

        if want_stream:
            return StreamingResponse(run_stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache",
                                              "X-Accel-Buffering": "no"})

        async for _ in run_stream():
            pass
        content = "".join(result["collected"])
        counted = result["counted"]
        return JSONResponse({
            "id": "chatcmpl-naigate", "object": "chat.completion",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": estimate_tokens(input_text),
                      "completion_tokens": max(counted, 1),
                      "total_tokens": estimate_tokens(input_text) + max(counted, 1)},
        })


# ================================================================ 页面 =======

@app.get("/healthz")
async def healthz():
    return {"ok": True, "upstream": STATE.nai.configured if STATE else False}


@app.get("/")
async def index():
    p = SETTINGS.announcement_path
    if p.exists() and p.read_text(encoding="utf-8").strip():
        return Response(p.read_text(encoding="utf-8"), media_type="text/html")
    return Response(DEFAULT_ANNOUNCEMENT, media_type="text/html")


@app.get("/admin")
async def admin_page():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/user/subscription")
async def user_subscription(request: Request):
    """给原生 NAI 客户端的虚拟订阅视图，不暴露真实上游账户余额。"""
    key = await authenticate(request)
    expires_at = float(key["expires_at"] or (time.time() + 3650 * 86400))
    return {
        "tier": 3,
        "active": True,
        "expiresAt": int(expires_at),
        "trainingStepsLeft": {
            "fixedTrainingStepsLeft": 0,
            "purchasedTrainingSteps": 0,
        },
        "perks": {
            "imageGeneration": True,
            "unlimitedImageGeneration": False,
            "voiceGeneration": False,
            "contextTokens": 0,
        },
        "usage": {
            "percent": 100,
            "isNegative": False,
            "timeUntilNextPercent": 0,
        },
        "naiGate": {
            "imageModelScope": key["image_model_scope"],
            "anlasEnabled": bool(key["allow_anlas"]),
        },
    }


# ================================================================ 路由注册 ====

for path in ("/ai/generate-image", "/nai/ai/generate-image"):
    app.post(path)(generate_image)
for path in ("/ai/generate-image/suggest-tags", "/nai/ai/generate-image/suggest-tags"):
    app.post(path)(suggest_tags)
    app.get(path)(suggest_tags)
for path in ("/ai/generate-image-stream", "/nai/ai/generate-image-stream"):
    app.post(path)(generate_image_stream_unavailable)
for path in ("/ai/generate-stream", "/nai/ai/generate-stream"):
    app.post(path)(generate_stream)
for path in ("/ai/generate", "/nai/ai/generate"):
    app.post(path)(generate_text)
for path in ("/ai/generate-voice", "/nai/ai/generate-voice"):
    app.post(path)(generate_voice)
app.get("/v1/models")(v1_models)
app.post("/v1/chat/completions")(v1_chat)
app.get("/v1/me")(v1_me)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SETTINGS.host, port=SETTINGS.port)
