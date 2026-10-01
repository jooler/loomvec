# MinerU 3.4.5 → 4.0.10 升级方案记录

> 2026-10-02。docs/ 只记现状；本文记录升级时的方案选择逻辑与权衡，供审查与后续演进参考。
> 上游依据：tag `mineru-4.0.10-released`（发布流程按 tag 名重写 `mineru/version.py`，
> vendored 副本已按同一机制改为 4.0.10）；迁移指南
> `https://opendatalab.github.io/MinerU/reference/migration_4`。

## 4.0 的破坏性变化（与本项目相关的）

| 3.x | 4.x | 影响 |
|---|---|---|
| HTTP `/file_parse`（同步）、`POST /tasks`（异步） | V1：`/v1/uploads` → `/v1/parse/jobs` → `/v1/files/{id}/content` | `MineruClient` 全部重写 |
| extras `mineru[pipeline]` | 移除；base 含 onnxruntime，`[torch]`/`[full]` 可选 | Dockerfile 改装 base |
| backend：pipeline / vlm-transformers / vlm-vllm-engine | tier：flash / basic / standard / advanced | 配置 `mineru.backend` → `mineru.tier`；兼容层 model_version 映射重写 |
| `mineru.json` / `MINERU_TOOLS_CONFIG_JSON` | `$MINERU_HOME/config.yaml` + `MINERU_*` 通配 env override | 容器 env 改 `MINERU_HOME=/models`；`MINERU_MODEL_SOURCE` 恰好仍是 model.source 的 env 形态，继续有效 |
| content_list（扁平块，bbox 页面坐标） | structured_content（页分组块，bbox 归一化 0~1）；zip 内含 markdown/structured_content/middle_json/images | 客户端内拉平，管线与 layout.json 存储契约不变 |
| 产物表格 HTML | Markdown 中仍渲染 HTML `<table>`（docvortex） | `chunking.py` 表格独立成片逻辑零改动 |
| `mineru-models-download -m pipeline` | `mineru-models-download --tier basic` | entrypoint 更新 |

## 关键决策与理由

1. **CPU 部署固定 `--tier basic`（暴露 flash+basic）**：standard/advanced 需 VLM
   引擎（linux 上 vllm，要 GPU），CPU 容器装不了；`required_modules_for_tier`
   预检也过不了。basic ≈ 旧 pipeline（本地小模型），CPU 上 small_backend 自动
   解析为 onnx（auto 规则：torch 可用且非 CPU 设备才选 torch）。
2. **不装 torch**：CPU 上 onnx 即可，镜像显著缩小。GPU 部署属未来项（届时改装
   `mineru[torch]` 并把 `--tier` 提为配置）。
3. **`MineruClient` 保持门面签名、重写内部**：`parse` / `submit_task` /
   `get_task` / `fetch_result_zip` / `health` 签名尽量稳定，兼容层与其测试
   （打桩在门面上）不必大改。`get_task` 把 V1 状态归一化回内部词汇
   （queued→pending、running→processing、partial 按文件成败映射），兼容层
   `_INTERNAL_STATE_MAP` 原样保留。
4. **structured_content 在客户端拉平为扁平块**：管线的 `build_line_page_map`
   只消费 `text`/`table_body`+`page_idx`+`bbox`，不值得为此引入 mineru 依赖
   （重达 torch）或在 loomvec 里移植上游渲染器。bbox 由页面坐标变为归一化
   坐标——下游只透传存储、不做数学，语义变化记录在 docs/03。
   captions/footnotes 拉平为独立块以保住行号匹配覆盖率。
5. **兼容层 page_range 升级为官方完整语法透传**：4.x 原生支持
   `'1-5,8,r3-r1'`/`'all'`，旧的「仅 N/N-M」差异消除；兼容层只做语法校验，
   语义校验（越界、倒序）归服务端。
6. **`language` / `enable_formula` / `enable_table` 接受不透传**：V1 任务体无
   对应字段（4.x 自动语言检测）。兼容层作为官方 API 兼容面继续接受这些字段，
   避免第三方客户端 422。
7. **同步解析走轮询**：V1 无同步端点，`parse()` 上传后轮询 job 至终态
   （2s 间隔，受 `timeout_seconds` 约束）。旧的"单请求挂 30 分钟"变为
   短请求轮询，对超时与重试更友好。
8. **vendored 副本整体替换**：3.4.5 副本经 diff 验证与上游 tag 逐字节一致
   （无本地补丁），故直接整目录替换为 4.0.10 tag 内容，沿用既有 vendoring
   方式（含 .github/docs/tests 全量）。

## 已知残留

- `openapi.json` 快照与 app 契约存在**存量漂移**（论文 auto-rename / 公共空间
  虚拟 viewer 等此前提交引入，与本次升级无关），需单独跑
  `uv run python scripts/export_openapi.py` 并同步 sdk-ts。
- DB 依赖类测试（admin_matrix / integration_units 等）需要 compose 栈
  （PG :35433）运行；本机 docker 未启动时失败，属环境性失败。
- standard/advanced（VLM）档位服务端未部署；兼容层 `model_version=vlm` 在
  该部署上按设计降级为配置档位并记告警日志。

## 后续演进（2026-10-02，同日）：vendored 恢复为依赖形态

升级时按既有 vendoring 方式整体替换了 `third_party/mineru`；随后复核确认
无论 3.4.5 还是 4.0.10 副本都与上游 tag 逐字节一致（唯一差异是 version.py
按上游发布机制重写），且项目不 import mineru（唯一消费方是解析镜像构建），
vendor 没有承载任何本地改动——每个 clone 背着 500+ 文件的第三方源码纯属负担。

**最终形态：PyPI 固定版本依赖**。`deploy/compose/mineru/Dockerfile`
`ARG MINERU_VERSION=4.0.10` + `pip install mineru==${MINERU_VERSION}`，
构建时拉取；`third_party/mineru` 移出库并 gitignore。选依赖而非
「deploy.sh 拉源码 tarball」的原因：镜像构建本就需要网络（apt/pip），
pip 直接拿 wheel（版本号正确，省掉 tag 里 version.py 落后一版的改写），
无需 checksum/镜像源维护；China 网络走 pip 镜像源是既有能力。
License 合规不受影响（wheel dist-info 携带 LICENSE.md，docs/13 复核锚点
改为 MINERU_VERSION）。升级步骤 = 改 Dockerfile 一处版本号 + 重跑解析冒烟。
