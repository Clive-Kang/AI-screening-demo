# CODE_MAP.md — 代码地图

> 本文件只回答三个问题：**这个文件干什么？它依赖谁？它写了没有？**
> 设计理由 → [ARCHITECTURE.md](ARCHITECTURE.md)；进度与工作约定 → [CLAUDE.md](CLAUDE.md)。
> **每完成一轮开发，更新本文件的「状态」列。**

图例：`✅` 已完成　`🔄` 进行中　`⬜` 未开始

---

## 0. 一眼看进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 阶段 1 | 领域内核 + 配置化评分 | 🔄 **进行中（5/7 轮）** |
| 阶段 2 | 持久化 + 端口/适配器 | ⬜ 未开始 |
| 阶段 3 | 异步任务（含上传异步化 §5.5） | ⬜ 未开始 |
| 阶段 4 | LLM 层加固 | ⬜ 未开始 |
| 阶段 5 | 检索下推 + 语义分并入 | ⬜ 未开始 |
| 阶段 6 | 横切收尾 | ⬜ 未开始 |

---

## 1. 当前真实文件树

```
D:\AI_resume_screening_self\
├── app/
│   ├── __init__.py                  ✅ R1   包标记 + 版本号
│   └── domain/
│       ├── __init__.py              ✅ R1   依赖边界声明（禁止 import 框架）
│       ├── text.py                  ✅ R2-R3 日期归一 + 在职标记 + 经历时长归并
│       ├── policies.py              ✅ R4   评分档案：YAML → 校验过的对象
│       └── models.py                ✅ R5   领域模型：日期区间 / 经历 / 学历 / 评分输出
├── config/
│   └── scoring.yaml                 ✅ R4   评分档案配置（tech / sales / strict）
├── tests/
│   ├── test_smoke.py                ✅ R1   脚手架冒烟
│   └── unit/
│       ├── test_text.py             ✅ R2-R3 日期解析、月数、区间归并（58 个用例）
│       ├── test_policies.py         ✅ R4   权重校验、继承、非法配置必须报错（16 个用例）
│       └── test_models.py           ✅ R5   模型不变量与派生属性（21 个用例）
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
| `scoring.py` | 六维 + 语义维加权，每维返回 `(score, weight, reason)` | models / policies / text | services | ⬜ R6 |
| `filtering.py` | 硬性过滤：经验、学历、地点；三种 `unknown_field_policy` | models / policies / text | services | ⬜ R7 |

### 3.2 `tests/` —— 跟着模块走

| 文件 | 测什么 | 状态 |
|---|---|---|
| `test_smoke.py` | 包能导入、阶段 1 依赖版本可用 | ✅ R1 |
| `unit/test_text.py` | 日期解析、在职标记、月数、区间归并（58 个用例） | ✅ R2-R3 |
| `unit/test_policies.py` | 权重和 == 1.0、档案继承、非法配置必须报错 | ✅ R4 |
| `unit/test_models.py` | 模型不变量、派生属性、`coverage` 计算 | ✅ R5 |
| `unit/test_scoring.py` | 各维度得分与 `reason` 文案、权重加权正确 | ⬜ R6 |
| `unit/test_filtering.py` | 三种 `unknown_field_policy` 的行为差异 | ⬜ R7 |

### 3.3 阶段 2 起才会出现的文件（位置已定，暂不建）

| 文件 | 干什么 | 阶段 |
|---|---|---|
| `app/ports/*.py` | 接口定义：llm / vector_store / repository / task_queue / blob_store / document_parser | 2 |
| `app/adapters/parser/{pdf,text}_parser.py` | 文件 → 纯文本 | 2 |
| `app/adapters/repo/sqlite_repo.py` | SQLite 读写（唯一真源） | 2 |
| `app/adapters/vector_store/sqlite_vec_adapter.py` | 向量索引（v1 默认实现） | 2 |
| `app/db/schema.sql` | 表结构（ARCHITECTURE.md §4.2） | 2 |
| `app/services/ingestion.py` | 上传编排：解析 → 抽取 → 落库 → 建索引 | 2 |
| `app/adapters/llm/openai_compat.py` | DeepSeek / Ollama 调用，含重试、超时、并发 | 4 |
| `app/adapters/queue/sqlite_queue.py` | SQLite 当任务队列 | 3 |
| `app/worker.py` | 独立进程消费队列（上传处理 + 筛选） | 3 |
| `app/services/screening.py` | 筛选编排：召回 → 过滤 → 评分 → 排序 → 分析 | 3 |
| `app/api/routes/*.py` + `deps.py` | HTTP 接口与依赖注入 | 3 |
| `app/main.py` | 组合根：构造 adapters 并注入 services | 3 |
| `app/config/settings.py` | `pydantic-settings`，启动校验 | 2 / 4 |
| `app/cli.py` | `reindex` / `rebuild-metadata` 运维命令 | 5 |
| `app/prompts/*.v1.txt` | 外置 prompt 模板，带版本号 | 4 |

---

## 4. 数据在每一步长什么样

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
