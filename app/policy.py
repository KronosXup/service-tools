"""核心策略：Opus 免费档判定、Anlas 估算、参数钳制、token 估算。

参考（社区逆向公式 + 官方博客，估算用）：
- https://tapwavezodiac.github.io/novelaiUKB/Image-Generation.html
- https://blog.novelai.net/subscription-updates-usage-limits-...-88a208d5d9c5

== 2026-08 V5 发布后的额度现实 ==

* V4.5 及更老模型：本网关仅将三种 Normal 规格（832x1216 / 1216x832 /
  1024x1024）的单张纯文生图判为 0 Anlas；其余规格按 Anlas 估算。
* V5 (nai-diffusion-5)：不再参与上述无限免费档！
  - Opus 有独立的「V5 周额度」：约 1800 张 Normal/周，服务端按 ~0.5%/小时恢复
    （约 190 张/天）。额度内的 Normal(<=28步) 生成不扣 Anlas。
  - 额度耗尽或超出条件时按 Anlas 计费，实测默认设置：Small 约 11A、
    Normal(1024px) 约 26A、Large 约 39A —— 约为 V4 公式的 ~1.3 倍。
  - 本网关无法读取 NovelAI 服务端的 V5 额度余量，因此用「全站每日 V5 张数」
    计数器来镜像它（默认 150 张/天，低于 190 的恢复量，留安全边际）。
* Anlas：订阅每月账单日「回满」到档位额度（Opus=10000），不叠加、不按天恢复。

估算公式（V3+ 通用）：
  per_image = ceil((2.951823174884865e-6 * r + 5.753298233447344e-7 * r * steps)
                   * smea_factor)
  1024x1024 / 28 steps -> 20 Anlas（与社区实测一致）；V5 再 x1.3。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Optional, Tuple

V5_COST_MULTIPLIER = 1.3  # V5 实测价格 / V4 公式 ≈ 1.3（11/26/39 vs 8/20/44）

# 仅允许明确认识的图片模型族。不能把未知模型当作旧模型，否则上游新增模型时
# 可能绕过 V5/Anlas 的保护逻辑。
LEGACY_IMAGE_MODEL_PREFIXES = (
    "nai-diffusion-4-5", "nai-diffusion-4", "nai-diffusion-3", "nai-diffusion-2",
    "nai-diffusion-furry-3",
)
LEGACY_IMAGE_MODEL_EXACT = {
    "safe-diffusion", "nai-diffusion", "nai-diffusion-furry",
}

# V5 官方「Normal」预设（用户实测：这三个尺寸走周额度、不扣 Anlas）。
# 判定规则：逐维小于等于任一预设即视为额度内（涵盖 Small 等更小尺寸）；
# 面积达标但非预设的自定义尺寸（如 896x1152）按额度外计费（保守，防止真实 Anlas 被扣）。
V5_NORMAL_PRESETS = ((832, 1216), (1216, 832), (1024, 1024))


# ---------------------------------------------------------------- tokens ----

def estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中英混合，1 token ≈ 3.5 字符）。仅用于配额预检。"""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 3.5))


def is_v5_model(model: str) -> bool:
    m = (model or "").lower()
    return m in ("nai-diffusion-5", "nai-v5") or m.startswith(
        ("nai-diffusion-5-", "nai-v5-")
    )


def image_model_tier(model: str) -> Optional[str]:
    """返回 legacy / v5；未列入白名单的模型返回 None。"""
    m = (model or "").strip().lower()
    if is_v5_model(m):
        return "v5"
    if (m in LEGACY_IMAGE_MODEL_EXACT or
            any(m == prefix or m.startswith(prefix + "-")
                for prefix in LEGACY_IMAGE_MODEL_PREFIXES)):
        return "legacy"
    return None


# ------------------------------------------------------------ 图片计费 ----

def _per_image_cost(width: int, height: int, steps: int,
                    smea: bool, smea_dyn: bool) -> int:
    """V3+ 通用单张价格（Anlas）。1024x1024/28steps -> 20A。"""
    r = width * height
    smea_factor = 1.4 if (smea and smea_dyn) else (1.2 if smea else 1.0)
    base = 2.951823174884865e-6 * r + 5.753298233447344e-7 * r * steps
    return max(2, math.ceil(base * smea_factor))


def _has_paid_extras(params: dict) -> bool:
    p = params.get("parameters", params)
    if p.get("controlnet_model") or p.get("controlnet_condition"):
        return True
    # V4/V5 角色参考 / 精确参考按张额外收费
    if p.get("characterReferences"):
        return True
    return False


def opus_free_eligible(params: dict) -> bool:
    """是否符合单张、纯文生图、低步数、无附加付费功能的基础条件。"""
    p = params.get("parameters", params)
    if int(p.get("n_samples", 1) or 1) != 1:
        return False
    if p.get("image") or p.get("mask"):
        return False  # img2img / inpaint 不免费
    if _has_paid_extras(params):
        return False
    if int(p.get("steps", 28) or 0) > 28:
        return False
    if p.get("sm") or p.get("sm_dyn"):
        return False
    w = int(p.get("width", 0) or 0)
    h = int(p.get("height", 0) or 0)
    if w * h > 1024 * 1024:
        return False
    return True


def legacy_normal_free_eligible(params: dict) -> bool:
    """V4.5 及更老模型仅三种固定 Normal 规格免 Anlas。"""
    if not opus_free_eligible(params):
        return False
    p = params.get("parameters", params)
    w = int(p.get("width", 0) or 0)
    h = int(p.get("height", 0) or 0)
    return (w, h) in V5_NORMAL_PRESETS


def v5_allowance_eligible(params: dict) -> bool:
    """V5 是否走 Opus 周额度（不扣 Anlas）。

    形状条件与老模型相同（单张/纯文生图/<=28步/无SMEA/无付费特性），
    分辨率额外要求：逐维 <= 任一 Normal 预设（832x1216 / 1216x832 / 1024x1024）。
    """
    if not opus_free_eligible(params):
        return False
    p = params.get("parameters", params)
    w = int(p.get("width", 0) or 0)
    h = int(p.get("height", 0) or 0)
    return any(w <= pw and h <= ph for pw, ph in V5_NORMAL_PRESETS)


def snap_v5_preset(width: int, height: int) -> Tuple[int, int]:
    """按长宽比吸附到最近的 V5 Normal 预设。"""
    ar = width / max(1, height)
    if ar > 1.1:
        return 1216, 832
    if ar < 0.9:
        return 832, 1216
    return 1024, 1024


def estimate_image_cost(params: dict, is_opus: bool = True) -> dict[str, int]:
    """估算一次 /ai/generate-image 的消耗。

    返回 {"anlas": 扣多少 Anlas, "v5": 占多少个 V5 额度单位}。
    二者互斥：V5 符合额度条件时只占额度，不符合时只按 Anlas（x1.3）。
    """
    p = params.get("parameters", params)
    w = int(p.get("width", 832) or 832)
    h = int(p.get("height", 1216) or 1216)
    steps = int(p.get("steps", 28) or 28)
    smea = bool(p.get("sm"))
    smea_dyn = bool(p.get("sm_dyn"))
    n = max(1, int(p.get("n_samples", 1) or 1))

    per = _per_image_cost(w, h, steps, smea, smea_dyn)
    if p.get("image"):  # img2img 按强度折算
        strength = float(p.get("strength", 1.0) or 1.0)
        per = max(2, math.ceil(per * max(0.01, strength)))

    shaped = legacy_normal_free_eligible(params)

    if is_v5_model(str(params.get("model", ""))):
        if v5_allowance_eligible(params):
            return {"anlas": 0, "v5": 1}   # 走 Opus 的 V5 周额度
        return {"anlas": max(1, math.ceil(per * n * V5_COST_MULTIPLIER)), "v5": 0}

    total = per * n
    if is_opus and shaped:
        total -= per  # 老模型：Opus 免费档扣掉首张
    return {"anlas": max(total, 0), "v5": 0}


def estimate_image_anlas(params: dict, is_opus: bool = True) -> int:
    """兼容旧接口：只返回 Anlas 部分。"""
    return estimate_image_cost(params, is_opus)["anlas"]


# ------------------------------------------------------------ 图片钳制 ----

def fit_size(width: int, height: int, max_pixels: int) -> Tuple[int, int]:
    """等比缩小到像素面积上限内，边长取 64 的倍数。"""
    if width * height <= max_pixels:
        return width, height
    scale = math.sqrt(max_pixels / (width * height))
    w2 = max(64, int(round(width * scale / 64)) * 64)
    h2 = max(64, int(round(height * scale / 64)) * 64)
    while w2 * h2 > max_pixels and (w2 > 64 or h2 > 64):
        if w2 >= h2 and w2 > 64:
            w2 -= 64
        elif h2 > 64:
            h2 -= 64
        else:
            break
    return w2, h2


def clamp_image_params(payload: dict, *, max_pixels: int, max_steps: int,
                       allow_img2img: bool) -> Tuple[dict, list[str], Optional[str]]:
    """把请求改写进「V5 额度条件 / 老模型免费档」的形状。

    返回 (新payload, 变更说明列表, 错误)。
    错误非 None 表示该请求被拒绝（例如 img2img 未开放）。
    对 V5：钳制后可走周额度（不烧 Anlas）；对老模型：钳制后直接免费。
    """
    notes: list[str] = []
    out = copy.deepcopy(payload)
    p = out.get("parameters")
    if not isinstance(p, dict):
        p = {}
        out["parameters"] = p

    # img2img / inpaint 审查
    is_img2img = bool(p.get("image") or p.get("mask"))
    if is_img2img and not allow_img2img:
        return out, notes, "本站未开放 img2img / 局部重绘（该功能会消耗 Anlas）"

    # 批量张数 -> 1
    if int(p.get("n_samples", 1) or 1) != 1:
        p["n_samples"] = 1
        notes.append("n_samples 已强制为 1（免费档/额度条件仅限单张）")

    # steps -> 上限
    if int(p.get("steps", 0) or 0) > max_steps:
        p["steps"] = max_steps
        notes.append(f"steps 已钳制到 {max_steps}")

    # 分辨率：免费 Key 一律吸附到三种 Normal 预设，确保不产生 Anlas。
    w = int(p.get("width", 0) or 0)
    h = int(p.get("height", 0) or 0)
    if w > 0 and h > 0:
        if is_v5_model(str(out.get("model", ""))):
            eligible = any(w <= pw and h <= ph for pw, ph in V5_NORMAL_PRESETS)
        else:
            eligible = (w, h) in V5_NORMAL_PRESETS
        if not eligible:
            pw, ph = snap_v5_preset(w, h)
            if (w, h) != (pw, ph):
                p["width"], p["height"] = pw, ph
                notes.append(f"分辨率 {w}x{h} 已吸附到 Normal 预设 "
                             f"{pw}x{ph}（避免产生 Anlas）")

    # SMEA -> 关闭（额外计费）
    if p.get("sm") or p.get("sm_dyn"):
        p["sm"] = False
        p["sm_dyn"] = False
        notes.append("SMEA 已关闭（会产生额外 Anlas 消耗）")

    # ControlNet / 角色参考（额外计费）
    if p.get("controlnet_model") or p.get("controlnet_condition"):
        return out, notes, "本站未开放 ControlNet（该功能会消耗 Anlas）"
    if p.get("characterReferences"):
        p.pop("characterReferences", None)
        notes.append("已移除角色参考（精确参考会消耗 Anlas）")

    return out, notes, None


# ------------------------------------------------------------ 文本钳制 ----

def clamp_text_params(payload: dict, *, max_output_tokens: int,
                      max_input_chars: int) -> Tuple[dict, list[str], Optional[str]]:
    notes: list[str] = []
    out = copy.deepcopy(payload)
    p = out.get("parameters")
    if not isinstance(p, dict):
        p = {}
        out["parameters"] = p

    input_text = str(out.get("input", "") or "")
    if len(input_text) > max_input_chars:
        return out, notes, f"输入过长（{len(input_text)} 字符，上限 {max_input_chars}）"

    ml = int(p.get("max_length", 0) or 0)
    if ml <= 0:
        p["max_length"] = min(150, max_output_tokens)
    elif ml > max_output_tokens:
        p["max_length"] = max_output_tokens
        notes.append(f"max_length 已钳制到 {max_output_tokens}")
    min_l = int(p.get("min_length", 1) or 1)
    if min_l > p["max_length"]:
        p["min_length"] = p["max_length"]
    return out, notes, None


def text_model_host(model: str, modern_host: str, legacy_host: str) -> str:
    """Kayra/Erato/GLM/Xialong 等新模型走 text.novelai.net，老模型走 api.novelai.net。"""
    m = (model or "").lower()
    if any(k in m for k in ("kayra", "erato", "glm", "xialong")):
        return modern_host
    return legacy_host


# ------------------------------------------------------------ 工具 ----

def mask_token(token: str, keep: int = 6) -> str:
    if len(token) <= keep:
        return "*" * len(token)
    return token[:keep] + "…" + token[-4:]


def gen_key(prefix: str = "nai") -> str:
    import secrets
    return f"{prefix}-{secrets.token_urlsafe(24)}"
