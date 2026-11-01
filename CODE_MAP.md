# CODE_MAP.md — 代码地图

> 本文件只回答三个问题：**这个文件干什么？它依赖谁？它写了没有？**
> 设计理由 → [ARCHITECTURE.md](ARCHITECTURE.md)；进度与工作约定 → [CLAUDE.md](CLAUDE.md)。
> **每完成一轮开发，更新本文件的「状态」列。**

图例：`✅` 已完成　`🔄` 进行中　`⬜` 未开始

---

## 0. 一眼看进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 阶段 1 | 领域内核 + 配置化评分 | ✅ **完成（7/7 轮）** |
| 阶段 2 | 持久化 + 端口/适配器 + HTTP 层 | ✅ **R8-R17 全部完成**（含真实端到端验收：3 份 PDF → 召回 → 过滤 → 评分 → Top-2 报告） |
| 阶段 3 | 异步任务（含上传异步化 §5.5） | ⬜ 未开始 |
| 阶段 4 | LLM 层加固 | ⬜ 未开始 |
| 阶段 5 | 检索下推 + 语义分并入 | ⬜ 未开始 |
| 阶段 6 | 横切收尾 | ⬜ 未开始 |

---

## 0.5 我要改 X，去哪？

先看这张表，再进文件树。**绝大多数改造都落在一两个文件里**，
因为 ports 把"要什么能力"和"怎么实现"分开了。

| 我想…… | 改哪里 | 注意 |
|---|---|---|
| 换 LLM 供应商 | `adapters/llm/openai_compat.py` + `.env` 的 `LLM_*` | 端口是 `ports/llm.py`，签名不变 |
| 换嵌入模型 | `adapters/embedding/openai_compat.py` + `EMBEDDING_*` | 维度变了要重建向量索引 |
| 换向量库（Milvus/Qdrant） | 新建 `adapters/vector_store/xxx.py`，改 `worker.py` / `api/deps.py` 的装配行 | 实现 `VectorStorePort` 即可 |
| 换数据库（PostgreSQL） | 新建 `adapters/repo/xxx.py`，实现两个 Repository | SQL 只在 adapter 里 |
| 换消息队列（Redis） | 新建 `adapters/queue/xxx.py`，实现 4 个方法 | worker / 路由一行不改 |
| 调权重 / 召回数 / Top-N | `config/scoring.yaml` | 六维权重和必须 == 1.0；改完重启 |
| 加/改评分维度 | `domain/scoring.py` + `domain/models.py` + `scoring.yaml` | 维度名要在 `KNOWN_DIMS` 里 |
| 改某个维度的算法 | `domain/scoring.py` 对应的 `score_*` 函数 | 纯函数，可直接单测 |
| 改硬性过滤规则 | `domain/filtering.py` | 三种缺失策略在 `scoring.yaml` 里配 |
| 改提示词 | `app/prompts/*.v1.txt` | 升级请新建 `*.v2.txt`，不要原地改 |
| 改上传/解析/入库流程 | `services/ingestion.py` | 状态机与失败落库都在这里 |
| 改召回/过滤/评分/报告顺序 | `services/screening.py` | 编排唯一入口 |
| 加一个 HTTP 接口 | `api/routes.py` | 只调 services，不写业务逻辑 |
| 看某个字段从哪来 | `app/db/schema.sql` → `adapters/repo/sqlite_repo.py` | 真源只有 SQLite |
| 改 worker 的失败重试策略 | `app/worker.py::_is_transient` | 永久失败进死信 |
| 改演示页面 | `web/index.html` | 单文件、无 CDN；页面只调 `/api/v1`，不碰数据库；岗位框下方可切换评分档案 |
| 换前端/加静态页 | `app/main.py` | 根路径 `GET /` 返回 `web/index.html`；其余仍是 JSON API |
| 清空/重置简历库 | `DELETE /api/v1/resumes`（[routes.py:118](app/api/routes.py#L118)） | 同时清原始文件、向量、队列任务；页面有按钮 |
| 一条命令起服务（开发/演示） | `scripts/dev.py 8010` | 起两个子进程，Ctrl+C 一起停；生产仍建议分开部署 |
| 跑一次端到端验收 | `scripts/smoke_test.py` | 先起服务；会真实调用 DeepSeek/Ollama |
| 造几份测试简历 | `scripts/make_samples.py` | 零依赖手写 PDF，输出到 `data/samples/` |
| 面试怎么讲这个项目 | `docs/project-brief.md` | 30 秒 / 3 分钟 / 展开版 + 六个技术难点 + 踩坑故事 |
| 面试可能被问什么 | `docs/interview-qa.md` | 10 类 60 问，含"为什么这样选 + 代价" |
| 录演示视频的口播稿 | `docs/demo-script.md` | 约 700 字，配操作节点的分镜 |

---

## 1. 当前真实文件树

```
D:\AI_resume_screening_self\
├── app/
│   ├── __init__.py                  ✅ R1   包标记 + 版本号
│   ├── domain/
│   │   ├── __init__.py              ✅ R1   依赖边界声明（禁止 import 框架）
│   │   ├── text.py                  ✅ R2-R3 日期归一 + 在职标记 + 经历时长归并
│   │   ├── policies.py              ✅ R4   评分档案：YAML → 校验过的对象
│   │   ├── models.py                ✅ R5   领域模型：日期区间 / 经历 / 学历 / 评分输出
│   │   │                            ✅ R8a  + Resume / ResumeStatus（简历本体与流水线状态）
│   │   │                            ✅ R8b  + Run / RunStatus / RunStage（筛选任务状态机）
│   │   ├── scoring.py               ✅ R6   七维打分 + combine（剔除分母 + 语义混合）
│   │   └── filtering.py             ✅ R7   硬性过滤 + 三种 unknown_field_policy
│   ├── adapters/
│   │   ├── __init__.py              ✅ R9b  适配器层边界（只有组合根可以 import 本层）
│   │   ├── repo/
│   │   │   ├── __init__.py          ✅ R9b  仓储实现包
│   │   │   ├── db.py                ✅ R9b  Database：PRAGMA / 事务 / 查询辅助
│   │   │   └── sqlite_repo.py       ✅ R9c-d SqliteResumeRepository + SqliteRunRepository
│   │   ├── llm/
│   │   │   ├── __init__.py          ✅ R11a LLM 实现包
│   │   │   └── openai_compat.py     ✅ R11a DeepSeek：JSON mode + 日期锚点 + 两类重试
│   │   ├── embedding/
│   │   │   ├── __init__.py          ✅ R11b 嵌入实现包
│   │   │   └── openai_compat.py     ✅ R11b Ollama bge-m3：批量 + 顺序归位 + 维度校验
│   │   ├── vector_store/
│   │   │   ├── __init__.py          ✅ R11c 向量库实现包
│   │   │   └── sqlite_vec_adapter.py ✅ R11c sqlite-vec：cosine + 阈值下推 + 只暴露 similarity
│   │   ├── queue/
│   │   │   ├── __init__.py          ✅ R15a 队列实现包（换 Redis 只改这里）
│   │   │   └── sqlite_queue.py      ✅ R15a tasks 表队列：可见性锁 + 幂等入队 + 死信
│   │   └── parser/
│   │       ├── __init__.py          ✅ R10b 解析器实现包
│   │       └── pdf_parser.py        ✅ R10b PdfParser（pypdf，magic bytes 校验）
│   ├── api/
│   │   ├── __init__.py              ✅ R16a HTTP 层边界（不写业务逻辑）
│   │   ├── deps.py                  ✅ R16a AppContext + Depends 注入（首个请求才建连接）
│   │   ├── schemas.py               ✅ R16a 响应 DTO（领域 dataclass → JSON 形状）
│   │   └── routes.py                ✅ R16a/b 全部路由：简历 / 筛选 / health / profiles
│   ├── config/
│   │   ├── __init__.py              ✅ R10a 说明与根目录 config/ 的分工
│   │   └── settings.py              ✅ R10a Settings：pydantic-settings 读 .env + 启动校验
│   ├── services/
│   │   ├── __init__.py              ✅ R13  服务层边界说明
│   │   ├── schemas.py               ✅ R13a LLM wire DTO（pydantic）→ 领域对象
│   │   ├── ingestion.py             ✅ R13b 解析→抽取→落库→嵌入→建索引
│   │   └── screening.py             ✅ R14b 召回→过滤→评分→排序→Top-N 报告（含幂等）
│   ├── prompts/
│   │   ├── __init__.py              ✅ R12  Prompt 加载器（string.Template + 版本号）
│   │   ├── resume_extract.v1.txt    ✅ R12  简历 → ResumeMetadata
│   │   ├── job_requirement.v1.txt   ✅ R12  HR 一句话 → JobRequirement
│   │   └── candidate_report.v1.txt  ✅ R12  候选人评估报告（Markdown 四段式）
│   ├── db/
│   │   └── schema.sql               ✅ R9a  11 张业务表（DDL 幂等，可重复执行）
│   ├── ports/
│       ├── __init__.py              ✅ R8a  端口层边界声明（禁止 import adapters / 框架）
│       ├── vector_store.py          ✅ R8a  VectorStorePort + Hit / ResumeDoc
│       ├── repository.py            ✅ R8b  ResumeRepository / RunRepository
│       ├── llm.py                   ✅ R8c  LLMPort（extract / generate，实现方注入日期锚点）
│       ├── document_parser.py       ✅ R8c  DocumentParserPort + ParsedDocument
│       ├── task_queue.py            ✅ R8c  TaskQueuePort + Task / TaskKind
│       └── embedding.py             ✅ R8c  EmbeddingPort（批量文本 → 向量）
│   ├── worker.py                    ✅ R15b 组合根 + 消费循环：分派两类任务、失败分类 nack
│   └── main.py                      ✅ R16a 组合根 + FastAPI app（import 阶段不读配置）
├── config/
│   └── scoring.yaml                 ✅ R4   评分档案配置（tech / sales / strict）
├── tests/
│   ├── test_smoke.py                ✅ R1   脚手架冒烟
│   └── unit/
│       ├── test_text.py             ✅ R2-R3 日期解析、月数、区间归并（58 个用例）
│       ├── test_policies.py         ✅ R4   权重校验、继承、非法配置必须报错（16 个用例）
│       ├── test_models.py           ✅ R5   模型不变量与派生属性（35 个用例，含 Run）
│       ├── test_scoring.py          ✅ R6   七维打分、剔除分母、语义混合（45 个用例）
│       ├── test_filtering.py        ✅ R7   四维硬性过滤、三种缺失策略（24 个用例）
│       ├── test_ports.py            ✅ R8a-c 值对象不变量 + 七个 Protocol 的契约（31 个用例）
│       ├── test_settings.py         ✅ R10a 必填、默认值、SecretStr 打码、范围校验（10 个用例）
│       ├── test_prompts.py          ✅ R12  模板可加载、占位符替换、漏传即报错（10 个用例）
│       ├── test_schemas.py          ✅ R13a 脏数据清洗、日期归一、to_domain（16 个用例）
│       ├── test_ingestion.py        ✅ R13b 状态推进、去重、失败落库、嵌入文本（10 个用例）
│       ├── test_screening.py        ✅ R14b 全流程、幂等三因子、partial、Top-N（13 个用例）
│       └── test_worker.py           ✅ R15b 分派、ack/nack、失败分类、真实队列联调（14 个用例）
├── tests/adapters/
│   ├── test_db.py                   ✅ R9b  建表幂等、外键级联、事务回滚（10 个用例）
│   ├── test_resume_repo.py          ✅ R9c  简历往返、拆表写入、更新替换、级联删除（16 个用例）
│   ├── test_run_repo.py             ✅ R9d  任务往返、阶段推进、取消保护、计数合并（13 个用例）
│   ├── test_pdf_parser.py           ✅ R10b supports 看内容、空白/缺失/损坏文件（6 个用例）
│   ├── test_llm.py                  ✅ R11a 日期锚点、JSON mode、校验重试、代码块剥离（15 个用例）
│   ├── test_embedding.py            ✅ R11b 顺序归位、数量/维度校验、异常翻译（8 个用例）
│   ├── test_sqlite_vec_store.py     ✅ R11c 检索/覆盖/阈值/删除/重建/持久化（13 个用例）
│   ├── test_results_repo.py         ✅ R14a 结果往返、淘汰留痕、语料版本指纹（10 个用例）
│   └── test_sqlite_queue.py         ✅ R15a 出入队、幂等、可见性锁、nack 与死信（13 个用例）
├── tests/api/
│   ├── conftest.py                  ✅ R16a 进程内 SQLite + 临时目录 + TestClient
│   ├── test_resumes.py              ✅ R16a 上传登记、去重、参数校验、列表/详情、health（13 个用例）
│   └── test_runs.py                 ✅ R16b 提交幂等、状态、结果分页、删除简历（14 个用例）
├── web/
│   └── index.html                   ✅ R17b 演示单页：上传 / 进度 / 结果卡片 / AI 报告（无构建、无 CDN）
├── scripts/
│   ├── make_samples.py              ✅ R17  生成三份测试简历 PDF（手写 PDF，无需 reportlab）
│   ├── smoke_test.py                ✅ R17  端到端验收：上传 → 等入库 → 提交 → 轮询 → 打印结果
│   └── dev.py                       ✅ R17c 开发启动器：一条命令起 API + worker 两个子进程
├── data/                            （运行时生成，已 gitignore）
│   ├── app.db / vectors.db / blobs/ ✅ R9   真源 + 向量索引 + 原始文件
│   └── samples/*.pdf                ✅ R17  验收样本（可重新生成）
├── requirements.txt                 ✅ R1   阶段 1 依赖（pydantic / PyYAML / pytest）
├── .gitignore / .gitattributes      ✅ R1
├── ARCHITECTURE.md                  ✅      设计文档（v1.1）
├── CLAUDE.md                        ✅      项目上下文 + 进度
└── CODE_MAP.md                      ←       本文件
```

---

## 2. 唯一的硬规则：依赖方向

**箭头 = "知道对方存在"**。反过来一律不允许。

```
      api  ─────────────▶ services ─────────────▶ domain
       │                     │                     ▲
       │                     ▼                     │
       └────────────────▶ ports ◀─────────────────┘
                            ▲
                       adapters        （只被 main.py / worker.py 装配时引用）
```

| 层 | 一句话职责 | 禁止 import |
|---|---|---|
| `api/` | 只做 HTTP：收请求、校验、序列化、返回 202 | adapters 的具体实现 |
| `services/` | 唯一知道"流程顺序"的地方 | 任何框架细节（只能通过 ports） |
| `domain/` | 纯规则：怎么算、怎么判 | **fastapi / langchain / chromadb / openai / 任何 adapter** |
| `ports/` | 只写"要什么能力"，不写"怎么实现" | adapters |
| `adapters/` | 实现 ports：SQLite、DeepSeek、向量库、PDF | services 的流程逻辑 |

> 记忆口诀：**domain 管"怎么算"，services 管"按什么顺序算"，adapters 管"东西从哪来、存到哪去"。**

---

## 3. 逐文件说明

### 3.1 `app/domain/` —— 纯规则层（零 IO，毫秒级可测）

| 文件 | 干什么 | import 谁 | 被谁用 | 状态 |
|---|---|---|---|---|
| `__init__.py` | 包标记；写明"禁止 import 框架"这条边界 | 无 | — | ✅ R1 |
| `text.py` | 日期归一、在职标记识别、多段经历时长（重叠取并集） | `re`、`collections.abc` | filtering / scoring / ingestion | ✅ R2-R3 |
| `models.py` | 领域数据结构：`DateRange` / `WorkExperience` / `Education` / `ResumeMetadata` / `DimScore` / `ScoreBreakdown` | 标准库 + `text.py` | 几乎所有模块 | ✅ R5 |
| `policies.py` | 评分档案对象：`ScoringProfile` / `UnknownFieldPolicy`；从 YAML 构造并校验 | pydantic + PyYAML | scoring / filtering / services | ✅ R4 |
| `scoring.py` | 六维打分函数 + `combine()`：剔除分母后按 `(1-w)` 与语义分混合，每维返回 `DimScore` | models / policies / text | services | ✅ R6 |
| `filtering.py` | 硬性过滤：经验 / 学历 / 地点 / 薪资；三种 `unknown_field_policy` | models / policies / scoring / text | services | ✅ R7 |

### 3.1b `app/ports/` —— 接口层（只有方法签名，没有实现）

| 文件 | 干什么 | import 谁 | 被谁用 | 状态 |
|---|---|---|---|---|
| `__init__.py` | 包标记；写明"禁止 import adapters / 框架" | 无 | — | ✅ R8a |
| `vector_store.py` | `VectorStorePort`（upsert / search / delete / drop_and_rebuild）+ `Hit` / `ResumeDoc` | 标准库 | services | ✅ R8a |
| `repository.py` | `ResumeRepository`（add/get/find_by_content_hash/list/update/delete）/ `RunRepository`（create/get/update_stage/finish/find_by_idempotency_key/list） | 标准库 + domain | services | ✅ R8b |
| `llm.py` | `LLMPort`（extract / generate，日期锚点由实现统一注入） | 标准库（pydantic 仅类型检查） | services | ✅ R8c |
| `document_parser.py` | `DocumentParserPort`（supports / parse）+ `ParsedDocument` | 标准库 | services | ✅ R8c |
| `task_queue.py` | `TaskQueuePort`（enqueue / dequeue / ack / nack）+ `Task` / `TaskKind` | 标准库 | services | ✅ R8c |
| `embedding.py` | `EmbeddingPort`（批量文本 → 向量） | 标准库 | services | ✅ R8c |

### 3.2 `tests/` —— 跟着模块走

| 文件 | 测什么 | 状态 |
|---|---|---|
| `test_smoke.py` | 包能导入、阶段 1 依赖版本可用 | ✅ R1 |
| `unit/test_text.py` | 日期解析、在职标记、月数、区间归并（58 个用例） | ✅ R2-R3 |
| `unit/test_policies.py` | 权重和 == 1.0、档案继承、非法配置必须报错 | ✅ R4 |
| `unit/test_models.py` | 模型不变量、派生属性、`coverage` 计算 | ✅ R5 |
| `unit/test_scoring.py` | 各维度得分与 `reason` 文案、剔除分母、语义混合、coverage | ✅ R6 |
| `unit/test_filtering.py` | 四维硬性过滤、三种 `unknown_field_policy` 的行为差异 | ✅ R7 |

### 3.3 阶段 2 起才会出现的文件（位置已定，暂不建）

| 文件 | 干什么 | 阶段 |
|---|---|---|
| `app/ports/{llm,repository,task_queue,document_parser}.py` | 其余接口定义（`vector_store.py` 已于 R8a 完成） | 2 |
| `app/adapters/parser/{pdf,text}_parser.py` | 文件 → 纯文本 —— ✅ `pdf_parser.py` 已于 R10b 完成，`text_parser.py` 未建 | 2 |
| `app/adapters/repo/sqlite_repo.py` | SQLite 读写（唯一真源） | 2 |
| `app/adapters/vector_store/sqlite_vec_adapter.py` | 向量索引（v1 默认实现） | 2 |
| `app/db/schema.sql` | 表结构（ARCHITECTURE.md §4.2）—— ✅ 已于 R9a 完成 | 2 |
| `app/services/ingestion.py` | 上传编排：解析 → 抽取 → 落库 → 建索引 | 2 |
| `app/adapters/llm/openai_compat.py` | DeepSeek / Ollama 调用，含重试、超时、并发 | 4 |
| `app/adapters/queue/sqlite_queue.py` | SQLite 当任务队列 | 3 |
| `app/worker.py` | 独立进程消费队列（上传处理 + 筛选） | 3 |
| `app/services/screening.py` | 筛选编排：召回 → 过滤 → 评分 → 排序 → 分析 | 3 |
| `app/api/routes.py` + `deps.py` | HTTP 接口与依赖注入 | 3 |
| `app/main.py` | 组合根：构造 adapters 并注入 services | 3 |
| `app/config/settings.py` | `pydantic-settings`，启动校验 —— ✅ 已于 R10a 完成 | 2 / 4 |
| `app/cli.py` | `reindex` / `rebuild-metadata` 运维命令 | 5 |
| `app/prompts/*.v1.txt` | 外置 prompt 模板，带版本号 | 4 |

---

## 4. 端到端流水线（目标形态）

> 图例：`✅` 已实现　`🔄` 本轮在做　`⬜` 未实现（括号里是计划阶段）
> 图中每个方块末尾标了**它落在哪个文件**——看图时可以直接对照代码。

### 4.0 进程与依赖全貌

```
┌───────────────────────── 进程 A：uvicorn ─────────────────────────┐
│  app/main.py（组合根：构造 adapters，注入 services）               │
│    └─ app/api/routes/*          HTTP：收请求、校验、返回 202         │
│         └─ app/services/*       编排：只按序调用 domain 与 ports     │
└───────────────┬───────────────────────────┬──────────────────────┘
                │ 入队（TaskQueuePort）      │ 读状态（RunRepository）
                ▼                           ▼
┌───────────────────────── 进程 B：worker ──────────────────────────┐
│  app/worker.py                                                     │
│    └─ app/services/*      同一套编排，换了个触发方式                 │
└───────────────┬───────────────────────────────────────────────────┘
                │ 只认接口
                ▼
┌────────────────────────── ports（本层）───────────────────────────┐
│  LLMPort · VectorStorePort · ResumeRepository · RunRepository      │
│  TaskQueuePort · DocumentParserPort · EmbeddingPort                │
└───────────────┬───────────────────────────────────────────────────┘
                │ 由 main.py / worker.py 在启动时决定塞哪个实现
                ▼
┌────────────────────────── adapters ───────────────────────────────┐
│  SQLite（真源）· sqlite-vec（派生索引）· DeepSeek/Ollama · PyMuPDF  │
└───────────────────────────────────────────────────────────────────┘
```

**一句话**：`api` 和 `worker` 是两扇门，`services` 是唯一的流程表，`domain` 是计算器，`ports` 是插座，`adapters` 是插头。

---

### 4.1 流水线 A · 简历入库（写路径）

```
【上游】PDF 文件
   │
   │ ① POST /api/v1/resumes                                    ⬜ 阶段 3
   │    校验 magic bytes（不是 PDF 直接 400）
   ▼
  ② 算 content_sha256 → 查重        ResumeRepository            ✅ R9
   │    ├─ 已存在 → 200 + { resume_id, deduplicated: true }，流程结束
   │    └─ 不存在 ↓
   ▼
  ③ 原文件落盘 blob_path + 插 resumes 行（status=queued）        ✅ R9
   │
   │    ⟵ 到这里 HTTP 已经返回 202，前端拿到 resume_id 去轮询
   ▼
  ④ 入队 { task: parse_resume, resume_id }     TaskQueuePort    ✅ R8c 端口 / ⬜ 实现（阶段 3）
   │
   ▼
┌──────────────── worker 消费 ──────────────────────────────────────┐
│                                                                    │
│  ⑤ 解析 PDF → raw_text           DocumentParserPort     ✅ R10b   │
│     └ status: queued → parsed                                     │
│                                                                    │
│  ⑥ LLM 结构化抽取                LLMPort.extract       ⬜ R11/阶段4│
│     └ raw_text → ResumeMetadata（日期已归一成 YYYY-MM）           │
│     └ status: parsed → extracted                                  │
│                                                                    │
│  ⑦ 拆表落库                      ResumeRepository       ✅ R9    │
│     └ resume_skills / resume_education / resume_experience        │
│       （这三张表是 SQL 下推的基础，ARCHITECTURE.md §7.4）          │
│                                                                    │
│  ⑧ 拼嵌入文本 → 1024 维向量       EmbeddingPort         ⬜ R11   │
│     └ "标题 + 技能 + 经历摘要"，不是整份 raw_text（太长且噪声大）   │
│                                                                    │
│  ⑨ 向量写入（同 id 覆盖）          VectorStorePort       ⬜ R11   │
│     └ status: extracted → indexed                                 │
│                                                                    │
│  ⑩ corpus_version += 1                                   ✅ R9    │
│     └ 简历库变了 → 旧 run 的幂等键自动失效，同 query 会重算       │
└────────────────────────────────────────────────────────────────────┘
   │
   ▼
【下游】resumes.status = indexed，可被筛选召回
```

**失败路径**：⑤~⑨ 任一步抛异常 → `status=failed` + `error` 落库，任务「死信」而不是无限重试（`TaskQueuePort.nack(retry=False)`）。

---

### 4.2 流水线 B · 筛选（读路径）

```
【上游】HR 的一句话："招 3 年 Python 后端，北京，本科以上"
   │
   │ ① POST /api/v1/runs                                        ⬜ 阶段 3
   ▼
  ② LLM 解析 → JobRequirement        LLMPort.extract       ⬜ R11/阶段4
   │    └ 技能/学历/地点/薪资/经验/行业/标签（models.py）
   ▼
  ③ 算幂等键                                                    ✅ R9
   │    key = sha256(query_text, profile, corpus_version)
   │    ├─ 命中既有 run → 直接返回它的 run_id，不重跑、不计费
   │    └─ 未命中 ↓
   ▼
  ④ 建 runs 行（status=queued）→ 返回 202            RunRepository ✅ R9
   │
   ▼
  ⑤ 入队 { task: screen_run, run_id }           TaskQueuePort   ⬜ 阶段 3
   │
   ▼
┌──────────────── worker 消费 ──────────────────────────────────────┐
│                                                                    │
│  ⑥ recall   向量检索 top_k=100, min_similarity=0.30                │
│             VectorStorePort.search             ⬜ R11 / 阶段 5    │
│             └→ [(resume_id, similarity), ...]                      │
│             └ stage=recall, progress={"recall": 100}               │
│                                                                    │
│  ⑦ filter   结构化条件走 SQL 下推（不是 Python 里筛）              │
│             ├ 经验 / 学历 / 地点 / 薪资       ✅ R7               │
│             ├ 三种 unknown_field_policy      ✅ R7                │
│             └ 淘汰者不丢：原因写进 FilterResult.reasons            │
│             └ stage=filter, progress={"filtered": 34}              │
│                                                                    │
│  ⑧ score    六维打分 + combine（剔除分母 + 语义混合）  ✅ R6      │
│             └→ ScoreBreakdown(overall, dims[], semantic, coverage) │
│             └ stage=score, progress={"scored": 34}                 │
│                                                                    │
│  ⑨ rank     按 overall 降序，同分比 coverage                       │
│                                                                    │
│  ⑩ analyze  仅 Top-N（默认 10）**并发**写报告   LLMPort.generate   │
│             ├ 超 token 预算 → 停止，status=partial（不是 failed）  │
│             └ stage=analyze, progress={"analyzed": 10}             │
│                                                                    │
│  ⑪ 落库 results 表（每候选人一行，含被淘汰者的 reject_reason）      │
│             第 10 名之后不调用 LLM —— 参考项目对全部候选人串行调用， │
│             这是它最贵的一步，而第 11 名之后的报告没人看            │
└────────────────────────────────────────────────────────────────────┘
   │
   ▼
【下游】两个只读接口，重复调用**不产生任何计算**
     GET /api/v1/runs/{id}          → status + stage + progress
     GET /api/v1/runs/{id}/results  → 排序结果（?include_rejected=true 看淘汰者）
```

**关键对照**：参考项目的 `GET /results/{id}` 每次调用都重跑全流程 + N 次 LLM；本项目路由里**只有 SELECT**。这也是 `RunStatus.PARTIAL` 存在的理由——预算超了不该让整次筛选作废。

---

### 4.3 单份简历的数据形态（写入路径简化版）

跟着**一份简历**走一遍，这是理解本项目最省力的路径：

```
① PDF 文件（二进制）
        │  adapters/parser
        ▼
② raw_text : str
   "张三 … 2020.07 – 2023.12 … Python / FastAPI …"
        │  domain/text.py::parse_year_month        ← R2 已完成
        ▼
③ start_ym = "2020-07"   end_ym = "2023-12"
        │  adapters/llm 结构化抽取
        ▼
④ ResumeMetadata { name, skills[], education[], work_experience[] }
        │  services/ingestion → 拆表落库
        ▼
⑤ SQLite：resumes / resume_skills / resume_education / resume_experience
        │  拼成一段可嵌入的文本
        ▼
⑥ vector : list[float]，1024 维（bge-m3）
        │  adapters/vector_store
        ▼
⑦ 向量索引：resume_id ↔ vector
```

### 4.4 筛选路径的数据形态（简化版）

筛选时再反着走一遍：

```
HR 的一句话
   │ LLM 解析
   ▼
结构化条件（技能/年限/学历/地点）
   │ 向量检索
   ▼
候选 resume_id + similarity
   │ SQL 下推过滤 + domain/filtering
   ▼
通过的候选人 + 淘汰原因
   │ domain/scoring
   ▼
ScoreBreakdown（每维 score/weight/reason）
   │ 排序取 Top-N + LLM 写报告
   ▼
results 表 → GET /runs/{id}/results
```

---

## 5. 「为什么要这一层」速查

| 你可能问 | 答案 |
|---|---|
| 为什么不直接在路由里写完？ | 参考项目就是这么干的，结果 GET 有副作用、无法单测、每次刷新都重新计费 |
| 为什么 domain 不许 import 框架？ | 一旦 import，这条规则就只能起服务才能测；而它恰好是最难写对的部分 |
| 为什么要有 ports？ | 换向量库/换 LLM 不改业务代码；测试时注入假实现，不发真实请求 |
| 为什么 SQLite 是唯一真源？ | 参考项目内存 + 向量库两套真源，重启后不一致 |
| 为什么上传要异步？ | 每份简历一次 LLM 调用，串行时 10 份要 2-5 分钟（ARCHITECTURE.md §5.5） |
