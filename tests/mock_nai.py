"""Mock NovelAI 上游，用于本地联调（不访问真实 API）。"""
import gzip
import io
import json
import zipfile

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI()


@app.post("/ai/generate-image")
async def gen_image(request: Request):
    body = await request.json()
    p = body["parameters"]
    # 返回一个真正的 zip（内含 PNG 头的假文件）
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(p.get("n_samples", 1)):
            z.writestr(f"image_{i}.png", b"\x89PNG\r\n\x1a\nFAKE")
    return Response(buf.getvalue(), media_type="binary/octet-stream")


@app.post("/ai/generate-stream")
async def gen_stream(request: Request):
    body = await request.json()
    ml = body.get("parameters", {}).get("max_length", 10)

    async def sse():
        for i in range(min(ml, 20)):
            yield f'data: {json.dumps({"token": f" word{i} "})}\n\n'
        yield "data: [DONE]\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream")


@app.post("/ai/generate")
async def gen_text(request: Request):
    body = await request.json()
    return JSONResponse({"output": " Once upon a time " * 3})
