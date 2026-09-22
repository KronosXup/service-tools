"""Client compatibility views derived only from the authenticated virtual key."""

from __future__ import annotations

import time
from datetime import datetime, timedelta


async def subscription_payload(state, key, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    key = dict(key)
    day, month = state.day(now), state.month(now)
    admin = bool(key.get("is_admin"))
    paid = admin or bool(key["allow_anlas"])
    monthly_limit = 0.0 if admin else float(key["monthly_anlas"] or 0)
    monthly_used = await state.db.month_anlas(key["id"], month)
    anlas_left = max(0, int(monthly_limit - monthly_used)) if paid and monthly_limit > 0 else 0

    limits: list[int] = []
    remaining: list[int] = []
    if not admin:
        daily_limit = int(key["daily_v5"] or 0)
        if daily_limit > 0:
            counter = await state.db.get_counter(key["id"], day)
            limits.append(daily_limit)
            remaining.append(max(0, daily_limit - int(counter["v5"])))
        if not key.get("exclude_global_v5"):
            global_limit = int(float(await state.db.get_setting(
                "global_daily_v5", state.settings.global_daily_v5) or 0))
            if global_limit > 0:
                limits.append(global_limit)
                remaining.append(max(0, global_limit - int(await state.db.day_v5_total(day))))
    v5_limit = min(limits, default=0)
    v5_left = min(remaining, default=0)
    percent = int(round(v5_left * 100 / v5_limit)) if v5_limit else 100
    local_now = datetime.fromtimestamp(now, state.tz)
    tomorrow = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    return {
        "tier": 3,
        "active": True,
        "expiresAt": int(key["expires_at"] or now + 3650 * 86400),
        "trainingStepsLeft": {
            "fixedTrainingStepsLeft": anlas_left,
            "purchasedTrainingSteps": 0,
        },
        "perks": {
            "imageGeneration": True, "unlimitedImageGeneration": False,
            "voiceGeneration": False, "contextTokens": 0,
        },
        "usage": {
            "percent": percent, "isNegative": False,
            "timeUntilNextPercent": max(0, int(tomorrow.timestamp() - now)),
        },
        "naiGate": {
            "imageModelScope": "all" if admin else key["image_model_scope"],
            "anlasEnabled": paid,
            "anlasLeft": anlas_left,
            "anlasMonthlyLimit": int(monthly_limit),
            "v5LeftToday": v5_left,
            "v5DailyLimit": v5_limit,
            "v5Unlimited": not limits,
            "isAdmin": admin,
            # Older panels accept null and hide shared account details.
            "account": None,
        },
    }
