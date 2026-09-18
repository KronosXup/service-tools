# NAI Gate —— NovelAI 公益分发网关

把你的 NovelAI Opus 订阅安全地分发给他人使用的**自建中转站**。

```
用户A(nai-xxx) ─┐
用户B(nai-yyy) ─┼─→  NAI Gate（你的服务器）        NovelAI
用户C(nai-zzz) ─┘    ├─ 鉴权：只认虚拟 Key          api.novelai.net
                     ├─ 限流：RPM / 并发 / 排队   ←─ 真实 Token 只存服务端
                     ├─ 配额：V5/Anlas/日tokens
                     ├─ 钳制：强制贴合 Opus 免费档（不烧 Anlas）
                     └─ 记账：每次调用入库，后台可视化
```

- 别人拿到的是你签发的**虚拟 Key**（`nai-xxxx`），你的真实 NovelAI Token 永远不出服务器
- 四层防护：每分钟请求数 → 单 Key 并发 → 全站并发 → 按日/按月配额
- 默认开启**安全钳制**：所有生图请求自动改写到 Opus 免费档（单张 / ≤28 步 / ≤1024×1024 / 关 SMEA），从机制上杜绝别人烧你的 Anlas
- 内置中文管理后台：发 Key、改配额、看用量日志、站点公告

---

## ⚠️ 先读我：风险与合规

**NovelAI 服务条款（5.3 / 9.1）明确禁止账号共享**，官方文档原话："Any request using your access token should be done by the owner of the account"，违规后果是**账号永久封禁或免费生成权限被暂停**。分发式使用属于灰色地带，请自行评估并采取以下缓解措施：

1. **用小号，不要用主号**：注册一个单独账号开 Opus 专门做公益站，封了不心疼；主号留着自己用
2. **控制总并发**：`GLOBAL_CONCURRENCY` 保持 1，模拟"单人在用"的行为特征（本项目默认值就是 1）
3. **不要挂公网大流量**：公益站小圈子分发即可，Writeup 里那种"人人可注册"的大站风险完全不同
4. Token 泄露 = 账号失守：服务器上锁好 `.env` 权限，后台密码用强密码

---

## 部署

### 方式一：Docker Compose（推荐）

```bash
git clone <本仓库> nai-gate && cd nai-gate
cp .env.example .env
vim .env          # 至少改 ADMIN_PASSWORD 和 NAI_TOKENS 两项
docker compose up -d --build
curl http://127.0.0.1:8000/healthz   # {"ok":true,...} 即成功
```

### 方式二：手动运行（Python ≥ 3.11）

```bash
pip install -r requirements.txt
export ADMIN_PASSWORD='你的后台密码'
export NAI_TOKENS='pst-你的持久Token'
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> 注意：限流器是进程内存实现，**必须单 worker 运行**（默认就是）。要上 HTTPS 请用 Nginx/Caddy 反代。

### 获取 NovelAI 持久 Token

1. 登录 [novelai.net](https://novelai.net)
2. 左侧边栏齿轮 → **User Settings** → **Account** 标签
3. 点击 **Get Persistent API Token**，复制（`pst-` 开头）
4. 填入 `.env` 的 `NAI_TOKENS=`（多个 token 用英文逗号分隔即组成令牌池，网关会轮询使用并在 429 时自动退避）

---

## 站长上手

1. 打开 `http://你的服务器:8000/admin`，用 `ADMIN_PASSWORD` 登录
2. **密钥管理 → 新建 Key**：先选图片模型范围（默认“仅 V4.5 及更低”；需要 V5 才选“所有当前支持模型”），再设置每日 V5、每日/月 Anlas、每日文本 tokens、RPM、有效天数
   - `允许消耗 Anlas`：默认关。关闭时，任何会花 Anlas 的请求直接被拒（402），双保险
   - `允许 img2img / 局部重绘`：默认关。该功能必烧 Anlas
3. 把生成的 `nai-xxxx` 发给用户，同时把你的站点地址发给他
4. **总览**页看全局用量与上游令牌池健康状态；**用量日志**页可按 Key 审计每一次调用（含被钳制的参数说明）
5. **设置**页可编辑首页公告（告诉用户怎么申请 Key、使用规则）

## 用户接入方式

虚拟 Key 用法与官方一致，把 `Authorization: Bearer <token>` 换成虚拟 Key、API 地址换成你的站点即可。

**① SillyTavern（酒馆）/ OpenAI 兼容客户端 —— 文本续写**

- API 类型：`自定义(OpenAI 兼容) / Custom (OpenAI-compatible)`
- Endpoint：`http://你的站点:8000/v1`
- Key：`nai-xxxx`
- 模型：`llama-3-erato-v1` / `kayra-v1` / `nai-glm-4-6` 等（`GET /v1/models` 可查）

**② 原生 NovelAI 客户端（支持自定义 API 地址的工具，如 ComfyUI 的 NAI 节点等）**

- 图片：`POST http://你的站点:8000/ai/generate-image`
- 文本流式：`POST http://你的站点:8000/ai/generate-stream`
- 语音：`POST http://你的站点:8000/ai/generate-voice`
- 请求体与官方 API 完全一致，直接把 base URL 从 `image.novelai.net` / `text.novelai.net` 改成站点地址

> **NAI Launcher v3 兼容**：第三方地址填写站点根地址（如 `https://your-domain.example`），不要加 `/v1`。已兼容 Bearer 登录所需的 `/user/subscription`、普通 ZIP 生图的 multipart 请求和 GET 标签建议；请关闭启动器的“流式预览”，本站不提供 `generate-image-stream`。

**③ curl 示例**

```bash
# 生图（会被自动钳制到免费档）
curl -X POST http://站点:8000/ai/generate-image \
  -H "Authorization: Bearer nai-xxxx" -H "Content-Type: application/json" \
  -d '{"input":"1girl, best quality","model":"nai-diffusion-4-5-full","action":"generate",
       "parameters":{"width":832,"height":1216,"steps":28,"n_samples":1,"sampler":"k_euler_ancestral"}}'
# 返回 zip（内含 png），与官方一致

# 查询自己的剩余额度
curl http://站点:8000/v1/me -H "Authorization: Bearer nai-xxxx"
```

---

## 2026-08 V5 发布后的额度现实（重要）

官方在 V5 发布时调整了政策（[官方博客](https://blog.novelai.net/subscription-updates-usage-limits-subscription-anlas-policy-adjustments-88a208d5d9c5)）：

| 资源 | 规则 |
|---|---|
| **V4.5 及更老模型** | 维持原状：Opus 下「单张 / 纯文生图 / ≤28 步 / ≤1024×1024 / 无 SMEA」→ **0 Anlas 不限量** |
| **V5 (nai-diffusion-5)** | **不再有无限免费档**。Opus 获得独立的「V5 周额度」：约 1800 张/周，服务端按 ~0.5%/小时（约 190 张/天）自动恢复；**账户内所有人共享**。实测三个 Normal 预设（竖屏 832×1216 / 横屏 1216×832 / 方形 1024×1024）及更小尺寸、≤28 步、单张 → 走周额度**不扣 Anlas**；额度外或更大尺寸按 Anlas 计费，实测 Small≈11A、Normal≈26A、Large≈39A（约为 V4 价格的 1.3 倍）。网关按**预设判定**（逐维 ≤ 任一预设），面积达标但非预设的自定义尺寸（如 896×1152）按额度外计费——宁紧勿松，防止真实 Anlas 被意外扣掉 |
| **Anlas** | 每月账单日**回满**到 10000（不叠加、不是每天恢复）；31 天后取消订阅时余额清零 |

网关无法读取 NovelAI 服务端的 V5 额度余量，因此用**全站每日 V5 张数**计数器来镜像恢复速率（默认 150/天 < 190/天，留安全边际），另外每把 Key 还有独立的「每日 V5 张数」限额。

## 推荐配额（2~3 人规模）

按「每月回满 10000、自己留 ~2500」计算，可送出约 7500 Anlas/月：

| 配额项 | 3 人规模（每人） | 2 人规模（每人） | 说明 |
|---|---|---|---|
| 每日图片 | 100 | 100 | V4.5 免费档不限成本，只防滥用 |
| 每日 V5 | 50 | 60 | 走官方周额度，不扣 Anlas |
| **每日 Anlas** | **80~100** | **120~150** | ≈3~4 张 V5 Normal 或 2~3 张 Large/天 |
| 每月 Anlas | 2300~2500 | 3400~4000 | 日限×30 应 ≤ 月限 |
| 全站月预算 | 7500 | 8000 | 后台「设置」里改 |
| 全站每日 V5 | 150 | 150 | 官方恢复速率 ~190/天 |

> 记住恒等式：**日限只是节奏控制，真正守恒的是月度**——`每日Anlas × 30 ≤ 每月Anlas ≤ 你愿意送的份额`。V5 很贵（Normal 26A），预算主要被 V5 吃掉；让朋友们日常用 V4.5 免费档 + 每天几张 V5，是最可持续的分法。

## 防爆费机制（重点）

V4.5 免费生图的条件是**同时满足**：单张（`n_samples=1`）、纯文生图（无底图/蒙版/ControlNet/角色参考）、`steps ≤ 28`、像素面积 ≤ 1024×1024、不开 SMEA；V5 相同形状的请求则走「V5 周额度」而不扣 Anlas。

| 防线 | 行为 |
|---|---|
| 安全钳制（默认开） | `n_samples` 强制 1、`steps` 钳到 28、超限分辨率等比缩小（64 的倍数）、强制关 SMEA、剥离 V4 角色参考；`img2img`/ControlNet 直接 400 拒绝 |
| `allow_anlas` 权限 | 即便钳制被管理员关闭，Key 没开通付费权限时，估算消耗 > 0 的请求一律 402 |
| 每日 Anlas 上限 | 开通 Anlas 的 Key 可设**每天最多花多少 Anlas**（0=不限），隔天自动恢复 |
| 月度 Anlas 配额 | 每把 Key 独立月度 Anlas 上限，按（社区逆向公式的）**估算值**预检+记账，超了 402 |
| 全站月度预算 | 所有 Key 共享的总闸（默认 10000，后台可改），封顶整站每月烧掉的 Anlas，对应订阅的每月返还量 |
| 日图片数配额 | 每把 Key 每日张数上限，超了 429 |

**关于"适当供给 Anlas"**：勾选 `允许消耗 Anlas` 的 Key **不再受免费档钳制**（否则永远花不出去），可以出大图、多张、img2img、ControlNet，但每一单都会被三道闸约束：`每日 Anlas` → `每月 Anlas` → `全站月度预算`。Opus 每月续期会把上月花掉的 Anlas 返还（上限 10000），所以建议：全站预算 ≈ 你愿意送出去的量，并留 20~30% 余量（估算公式与官方有偏差）。免费 Key 与 Anlas Key 可以混发，普通用户走免费档，信任的用户给 Anlas 额度。

文本生成对订阅用户不限量（只受单账号速率限制），网关按 **tokens/天** 限额，流式输出到达上限会**硬切断**并返回 `[DONE]`。

## 环境变量

见 [.env.example](.env.example)，全部有注释。常用的几项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_PASSWORD` | 无（后台锁定） | 管理后台密码 |
| `NAI_TOKENS` | 无 | 上游 Token，逗号分隔多个组成令牌池 |
| `GLOBAL_CONCURRENCY` | 2 | 全站并发（对上游的保护，**别调大**） |
| `KEY_CONCURRENCY` | 1 | 单 Key 并发 |
| `SAFE_CLAMP` | 1 | 安全钳制开关（对开通 Anlas 的 Key 不生效） |
| `ALLOW_IMG2IMG` | 0 | img2img 总开关（还需 Key 勾选 allow_img2img） |
| `GLOBAL_MONTHLY_ANLAS` | 10000 | 全站月度 Anlas 预算（后台可改） |
| `GLOBAL_DAILY_V5` | 150 | 全站每日 V5 张数（镜像官方 ~190/天恢复速率） |
| `KEY_INACTIVITY_DELETE_DAYS` | 3 | 超过天数未使用的 Key 自动永久删除；`0` 关闭 |
| `DEFAULT_*` | 60/500/150k/10/30 | 新 Key 默认配额，后台可逐 Key 覆盖 |
| `TZ` | Asia/Shanghai | 配额按天重置的时区 |

## 日常运维

- **上游 Token 失效**：总览页令牌池变红（401）→ 去官网重新生成 Token → 改 `.env` → `docker compose up -d` 重启
- **上游 429**：对应 Token 自动冷却 20s 并换池内其他 Token；全池冷却时用户收到 503
- **数据备份**：所有状态在 `data/nai_gate.db`（SQLite，WAL 模式），定期拷走即可
- **审计**：用量日志保留每次调用的 Key、类型、模型、状态、消耗和钳制说明

## 本地开发与测试

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -q                 # 20 个策略层单测（计费/钳制/配额）

# 起一个假上游做全链路联调（不访问真实 NovelAI）
python -m uvicorn tests.mock_nai:app --port 9999 &
NAI_TOKENS=fake NAI_IMAGE_HOST=http://127.0.0.1:9999 \
NAI_TEXT_HOST=http://127.0.0.1:9999 NAI_TEXT_HOST_LEGACY=http://127.0.0.1:9999 \
ADMIN_PASSWORD=test python -m uvicorn app.main:app --port 8000
```

## FAQ

**Q：Key 能创建多少个？会互相抢额度吗？**
A：数量不限，后台随时创建/禁用/删除。每把 Key 的配额**互相独立**（A 用完不影响 B），共享的只有两样：全站并发（默认 2，多人在用会排队）和全站月度 Anlas 预算。几十人规模的公益站完全够用。

**Q：限额是怎么实现的？**
A：RPM 用 60 秒滑动窗口（内存）预检；每日图片数/每日 Anlas/每日文本 tokens 按 `(Key, 日期)` 记在 SQLite，时区 Asia/Shanghai，隔天自然清零；文本是流式逐 token 计数、到上限硬切断；并发用「单 Key + 全站」两层信号量排队。**只有上游成功返回才记账**，失败请求不扣配额。

**Q：会影响我自己的使用吗？**
A：这正是架构要解决的问题——**强烈建议用小号**，物理隔离，主号零影响。如果坚持用主号 Token，把 `GLOBAL_CONCURRENCY` 设为 1 能保证不会和用户并发冲突，但配额共享、风险共担。

**Q：用户会用我的号看我的故事/图片吗？**
A：不能。网关只转发生成类接口（生图/生文/语音/打标），不透传 `/user/objects`（云端存储）等任何涉及你个人数据的接口；生成内容对 NovelAI 是端到端加密的，站长与用户互相看不到对方内容。

**Q：为什么不直接把官方 Token 给用户？**
A：官方 Token = 整个账号（包括余额、云存储、改密码前的所有权限），且无法限额。虚拟 Key 只能走生成接口，且配额、期限、功能权限全由你控制。

**Q：Opus 的文本不是无限吗，为什么还要限 tokens？**
A：无限指的是不限总量，但单账号有速率限制，且你的服务器带宽也有限。按 tokens 限额是为了防止单个用户把全站并发占满。
