# 15 - MinerU 官方 API 兼容层

> 目标：第三方既有 MinerU（mineru.net 官方 API）集成，把 `base_url` 从
> `https://mineru.net` 改为 loomvec、token 换成 loomvec 凭证，即可无缝获得
> loomvec 自建 MinerU 的解析能力——请求/响应信封、端点路径、状态机与官方一致。

## 一、背景

loomvec 的解析能力由自建 MinerU 提供（`deploy/compose` 中的 `mineru` 服务，
官方 `mineru-api`，端口 38000，仅内网），此前唯一调用方是摄取管线
（`MineruClient.parse()` → `POST /file_parse`，worker 进程内）。

本阶段把同一能力以 **mineru.net 官方「精准解析 API」（v4）** 的形态经 loomvec
API（38080）对外开放（`services/api/src/loomvec/api/routes/mineru_compat.py`）：

- 官方文档核对基准：`https://mineru.net/apiManage/docs`（2026-09）；
- 响应信封与官方一致：`{"code": 0, "data": {...}, "msg": "ok", "trace_id": "..."}`；
- 错误返回非 0 `code` + 描述性 `msg`（HTTP 状态码同步表达：400/401/403/404/409/413/429/502）。

## 二、端点一览

| 官方端点 | 说明 | loomvec 实现 |
|---|---|---|
| `POST /api/v4/extract/task` | 单文件 URL 解析（异步） | 下载 URL → 提交内部 mineru-api 异步任务 |
| `GET /api/v4/extract/task/{task_id}` | 任务状态/结果轮询 | 惰性轮询内部任务并映射状态 |
| `POST /api/v4/file-urls/batch` | 批量文件解析申请 | 返回 loomvec 直传 URL（≤50 文件，24h 有效） |
| `PUT /api/v4/file-urls/batch/{token}` | 文件直传 | 官方为预签名 OSS PUT；loomvec 侧 URL 即凭证、携带原始字节 |
| `POST /api/v4/extract/task/batch` | URL 批量解析 | 同单 URL 流程，按文件粒度后台提交 |
| `GET /api/v4/extract-results/batch/{batch_id}` | 批量结果轮询 | 逐文件惰性轮询 + 状态映射 |
| `GET /api/v4/extract-results/file/{token}` | 解析产物 zip 下载 | `full_zip_url` 指向此处，链接即凭证（24h） |

文件直传与 zip 下载端点不校验请求头鉴权（对齐官方"预签名 URL / 下载链接即凭证"
语义），令牌本身即凭证且 24h 过期；其余端点全部要求鉴权。

## 三、快速上手

```python
import requests

BASE = "http://localhost:38080"          # 原为 https://mineru.net
TOKEN = "<loomvec API Key>"              # admin 控制台 → 开放平台签发

headers = {"Authorization": f"Bearer {TOKEN}"}

# 流程一：本地文件批量解析（两步直传，同官方）
r = requests.post(f"{BASE}/api/v4/file-urls/batch", headers=headers, json={
    "model_version": "pipeline",
    "files": [{"name": "demo.pdf", "data_id": "doc-1"}],
}).json()["data"]
for url, path in zip(r["file_urls"], ["demo.pdf"]):
    requests.put(url, data=open(path, "rb").read())   # 直传后自动提交解析

# 轮询批量结果（状态：waiting-file / pending / running / done / failed）
res = requests.get(f"{BASE}/api/v4/extract-results/batch/{r['batch_id']}", headers=headers).json()
zip_url = res["data"]["extract_result"][0]["full_zip_url"]   # done 时返回
requests.get(zip_url)                                        # 完整产物 zip（md/content_list/images）

# 流程二：单文件 URL 解析
task = requests.post(f"{BASE}/api/v4/extract/task", headers=headers,
                     json={"url": "https://example.com/paper.pdf"}).json()["data"]
status = requests.get(f"{BASE}/api/v4/extract/task/{task['task_id']}", headers=headers).json()["data"]
```

## 四、语义映射

### 鉴权（关键差异点）

| 官方 | loomvec |
|---|---|
| `Authorization: Bearer <mineru token>` | `Authorization: Bearer <loomvec API Key>`（非 JWT 形态的 Bearer 一律按 API Key 解析） |
| — | `X-API-Key: <loomvec API Key>`（loomvec 惯例，同样接受） |
| — | 平台 Bearer JWT（dev/SSO 登录态）亦可 |

- API Key 身份：`read` scope 必需；限流沿用 API Key 的 key 级限流；
- 多租户隔离：任务/批次记录归属创建者租户，跨租户查询返回 404（不泄露存在性）。

### 状态机（与官方一致）

| 官方 state | 含义 | loomvec 来源 |
|---|---|---|
| `waiting-file` | 待文件直传 | 上传型批次文件未 PUT |
| `pending` | 排队中 | 已提交内部任务（mineru-api `pending`）或 URL 下载中 |
| `running` | 解析中 | 内部任务 `processing` |
| `done` | 完成（带 `full_zip_url`） | 内部任务 `completed` |
| `failed` | 失败（带 `err_msg`） | 内部任务 `failed` / 下载失败 / 提交失败 |

### 请求参数映射

| 官方参数 | 内部 mineru-api | 说明 |
|---|---|---|
| `model_version=pipeline` | `backend=pipeline` | 默认值（官方默认 vlm，见「差异」） |
| `model_version=vlm` | `backend=vlm-*` | 仅当部署配置 `mineru.backend` 为 VLM 时接受 |
| `is_ocr=true` | `parse_method=ocr` | 按文件粒度 |
| `language` | `lang_list=[language]` | — |
| `enable_formula` / `enable_table` | `formula_enable` / `table_enable` | 批次级 |
| `page_range` | `start_page_id` / `end_page_id` | 仅 `'N'` / `'N-M'` |
| `data_id` | — | 原样回显在批量结果中 |

### 产物

内部任务以 `response_format_zip=true` 提交，`done` 后签发一次性下载令牌，
`GET /api/v4/extract-results/file/{token}` 向内部 mineru-api
`GET /tasks/{task_id}/result` 拉取完整产物 zip（markdown + content_list + images）
原样流式返回——对齐官方 `full_zip_url` 的完整结果包语义。

## 五、实现要点

```
第三方客户端（官方 SDK / requests）
    │  Bearer <loomvec API Key>
    ▼
loomvec API :38080  services/api/src/loomvec/api/routes/mineru_compat.py
    │  /api/v4/*（include_in_schema=False：兼容面不进 loomvec 契约/SDK）
    │  状态：Redis mineru_compat:{task|batch|upload|download}:*（批次 72h / 令牌 24h）
    │  文件中转：对象存储 derived 桶 mineru-compat/{batch}/{index}（提交成功即删）
    ▼  复用 MineruClient（services/core/.../mineru_client.py）新增异步任务方法
自建 MinerU :38000  POST /tasks → GET /tasks/{id} → GET /tasks/{id}/result(zip)
```

- **惰性轮询**：客户端查询时才向内部 mineru-api 拉一次实时状态，无后台轮询
  进程；轮询失败保持现状态（不误判 failed）。
- **后台协程**：URL 下载、直传后自动提交均为进程内 `asyncio` 后台任务，
  任何异常都落为可见的 `failed` + `err_msg`。
- **上传幂等**：直传令牌用 Redis `SET NX` 防并发/重复上传（409）；4xx 校验
  失败不占用令牌，客户端可修正重传。
- **full_zip_url 的 host**：按请求头 `Host` / `X-Forwarded-Proto` /
  `X-Forwarded-Host` 生成，反代环境自动正确。
- **配置开关**：`config/loomvec.json` → `mineru.compat_enabled`（默认 `true`，
  置 `false` 完全关闭该端面）。

## 六、与官方的差异 / 局限

| 项 | 说明 |
|---|---|
| `model_version` 默认值 | loomvec 默认 `pipeline`（官方默认 `vlm`）；显式 `vlm` 在配置了 VLM 后端时按原语义处理，pipeline-only 部署上**降级为配置后端**（服务端记 `mineru_compat_vlm_degraded` 告警日志）而非 400——第三方客户端普遍携带官方默认值，硬拒绝会破坏开箱兼容 |
| `model_version=MinerU-HTML` | 云端专属能力，不支持（400） |
| `page_range` 多段语法 | 官方支持 `'1-3,5,8-10'`，自建 MinerU 仅有起止页概念，仅支持 `'N'` / `'N-M'` |
| Agent 轻量解析 API（`/api/v1/agent/*`，免鉴权） | 不提供：企业平台解析入口必须鉴权，且路径与 loomvec 自身 agent 路由冲突 |
| 结果保留时长 | 批次/任务记录 72h、上传/下载令牌 24h（官方结果保留 3 天，语义一致） |
| `extract_progress` | 官方运行中任务返回页级进度；自建 mineru-api 无此数据，不返回 |
| 直传 URL | 官方为 OSS 预签名 URL（客户端可直连云存储）；loomvec 为 API 进程接收再中转对象存储，功能等价 |
| 401 语义 | 明确区分两种情况：未携带凭证（"未认证：请在 Authorization: Bearer 头中携带 loomvec API Key…"）与凭证无效/过期（"凭证无效或已过期：<底层原因>"），便于客户端日志定位 |

## 六点一、已知客户端接入案例（InkCop）

InkCop（Qt 桌面端）经其 MinerU 设置的 `baseUrl` 直连本兼容层（官方两步直传 +
批量结果轮询 + zip 解压全流程）。接入时踩过两类问题，记录备查：

1. **发送密文 token**：InkCop 的 `app_settings` JSON 内 `apiKey` 字段落库为
   `enc:v1:...` 密文（SecretVault AES-GCM 逐字段加密），其 mineru/fileapi 读取
   侧曾未解密直接发送 → 401。已在 InkCop 侧修复（读取后经
   `SensitiveKeys::decryptJsonFields` 解密）。
2. **`model_version=vlm` 存量配置**：InkCop 存量设置默认写 `vlm`，对应上表的
   降级语义。

## 七、测试

`services/api/tests/test_mineru_compat.py`（18 例，免 infra：Redis / 对象存储 /
内部 mineru-api 全打桩）覆盖：单 URL 任务全流程（提交→轮询→zip 下载）、上传
批量全流程（申请→直传→自动解析→轮询）、重复直传 409、空/超限请求体、
`model_version` 降级与 `page_range` 校验、下载失败标记、租户隔离、Bearer 直带
API Key 的鉴权映射、无凭证 401 与凭证无效 401 的消息区分、`/api/v4` 不进
OpenAPI 契约。
