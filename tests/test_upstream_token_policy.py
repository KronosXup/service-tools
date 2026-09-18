"""特定上游令牌的免费图和 Anlas 隔离测试。"""

import asyncio
import os
import tempfile

from app.database import Database
from app.nai import NaiClient


def test_second_upstream_token_free_v5_limit_and_anlas_block():
    async def run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        db = Database(path)
        try:
            await db.connect()
            client = NaiClient(
                ["first-token", "second-token"], "https://image.example", "https://text.example",
                "https://legacy.example", db=db, day_fn=lambda: "2026-09-11",
                v5_daily_limits=[0, 150], allow_anlas=[True, False],
            )
            second = client.pool[1]

            # 第二把令牌绝不会承接需要 Anlas 的图片请求。
            assert (await client.pick_token(requires_anlas=True)) is client.pool[0]

            # 达到第二把的免费 V5 日限后，免费 V5 自动改由第一把承接。
            for _ in range(150):
                await db.bump_upstream_v5_counter(second.token_id, "2026-09-11")
            client._rr = 0  # 下一次轮询优先第二把，验证它确实被跳过。
            assert (await client.pick_token(v5_free=True)) is client.pool[0]

            # 非 V5 的免费图没有第二把的张数限制，仍可进入轮询。
            client._rr = 0
            assert (await client.pick_token()) is second

            # 上游实际成功生成量按成功响应的 n_samples 累加，不影响 V5 额度。
            await client.record_successful_images(second, 3)
            counter = await db.get_upstream_counter(second.token_id, "2026-09-11")
            assert counter == {"images": 3, "v5": 150}
        finally:
            await db.close()
            os.unlink(path)

    asyncio.run(run())
