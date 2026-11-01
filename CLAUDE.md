# CLAUDE.md — 智能简历筛选系统（重建版）

> 本文件承载跨会话的项目上下文。**新会话请先读本文件，再读 [ARCHITECTURE.md](ARCHITECTURE.md)（设计）与 [CODE_MAP.md](CODE_MAP.md)（代码地图）。**
> 回复语言：**始终使用简体中文**（代码、命令、变量名、日志、报错信息保持原文不翻译）。

---

## 0. 当前状态（截至 2026-10-18）

> **一句话进度**：**阶段 1、2 已完成，R17 端到端验收已通过**（3 份 PDF → 入库 → 召回 → 硬过滤 → 评分 → Top-2 报告，真实调用 DeepSeek + Ollama）。阶段 2 到此收尾。
> 工作约定见 §7；详细文件级进度以 [CODE_MAP.md](CODE_MAP.md) 为准；路线见 [ARCHITECTURE.md](ARCHITECTURE.md) §12。

### 已完成

| 轮次 | 内容 | 文件 | 存档点 |
|---|---|---|---|
| R1 | 项目脚手架 | `app/__init__.py`、`requirements.txt`、`tests/test_smoke.py` | `79b8011` |
| R2 | 日期写法归一化 | [text.py](app/domain/text.py)、[test_text.py](tests/unit/test_text.py) | `4a537bc` |
| R3 | 经历时长计算 | [text.py](app/domain/text.py)（`is_current_marker` / `months_between` / `merge_experience_months`） | `4c21348` |
| R4 | 评分配置加载与校验 | [scoring.yaml](config/scoring.yaml)、[policies.py](app/domain/policies.py)、[test_policies.py](tests/unit/test_policies.py) | `8a002d0` |
| R5 | 领域模型 | [models.py](app/domain/models.py)、[test_models.py](tests/unit/test_models.py) | `b817ad9` |
| R6 | 七维打分 + `combine()` | [scoring.py](app/domain/scoring.py)、[test_scoring.py](tests/unit/test_scoring.py)、`text.parse_salary_range` | 见下 |
| R7 | 硬性过滤 + 三种缺失策略 | [filtering.py](app/domain/filtering.py)、[test_filtering.py](tests/unit/test_filtering.py) | 见下 |

测试：`python -m pytest tests/ -q` → **445 passed**（命令必须带 `-m`，见 §7）

### 阶段 2 进度

| 轮次 | 内容 | 状态 |
|---|---|---|
| R8 | `app/ports/` 七个接口（vector_store / repository / llm / document_parser / task_queue / embedding） | ✅ |
| R9 | `db/schema.sql`（11 张表）+ `adapters/repo/{db,sqlite_repo}.py` | ✅ |
| R10 | `config/settings.py`（pydantic-settings）+ `adapters/parser/pdf_parser.py`（pypdf） | ✅ |
| R11 | `adapters/llm` + `adapters/embedding` + `adapters/vector_store`（sqlite-vec） | ✅ |
| R12 | `app/prompts/`：三个带版本号的 prompt 模板 + `Template` 渲染 | ✅ |
| R13 | `services/schemas.py`（wire DTO）+ `services/ingestion.py`（解析→抽取→入库→索引） | ✅ |
| R14 | results/corpus_version 落库 + `services/screening.py`（召回→过滤→评分→报告，含幂等） | ✅ |
| R15 | `adapters/queue/sqlite_queue.py`（可见性锁/死信）+ `app/worker.py`（组合根 + 消费循环） | ✅ |
| R16 | `api/`（resumes / runs / system）+ `deps.py` + `main.py`，`.env` / `.env.example` | ✅ |
| R17 | 端到端验收：[make_samples.py](scripts/make_samples.py) 造 3 份 PDF + [smoke_test.py](scripts/smoke_test.py) 跑全链路 | ✅ |
| R17b | 演示页 [web/index.html](web/index.html)：上传 / 简历状态 / 进度条 / 结果卡片 / 报告渲染 / 一键清空，`GET /` 直接打开 | ✅ |
| R17c | 开发启动器 [scripts/dev.py](scripts/dev.py)：一条命令起 API + worker（仍是两个进程，保留隔离） | ✅ |

**R17 实测结果**（2026-10-18，`deepseek-flash` + Ollama bge-m3）：
上传 3 份 → 全部 `indexed`（约 25 秒）→ 提交 `"招 Python 后端工程师，本科及以上，3 年以上工作经验"`
→ `succeeded`，`recall=3 / filtered(通过)=2 / scored=3 / analyzed=2`。
张三 0.920、王五 0.916 通过并生成报告；李四因「大专低于本科」被淘汰且保留原因。
同 query 再提交一次 → `deduplicated=true`，零额外 LLM 调用。

**验收暴露的两点已知限制**（不是 bug，但要知道）：
1. 样本简历是英文的（PDF 标准字体无中文），`Beijing` 与岗位里的`北京`字符串交集为 0 → 地点硬过滤误杀。
   真实中文简历不会触发；若要多语言支持，需在 `domain/filtering.py` 加地名归一表。
2. `progress.filtered` / `filtered_n` 的语义是「过滤后**通过**的人数」，不是「被淘汰数」——
   字段名沿用 SQL 列名，已在 `api/schemas.py` 的 Field description 里写明。

### 已定的技术决策

| 决策 | 选择 | 原因 |
|---|---|---|
| 开发环境 | 复用 conda `ai_resume_screening` | 阶段 1 依赖已齐全，零安装 |
| 脚手架 | `requirements.txt`，靠 cwd（同参考项目） | 最小心智负担；实测代价是命令必须带 `-m` |
| 只写到年的日期（`2020`） | 补成 `2020-01` | 输出永远是干净的 `YYYY-MM`，下游少一种状态 |
| 区间月数算法 | 差值法：`2025-08 → 2026-09` = 13 个月 | 依据 ARCHITECTURE.md §4.4 的例子 |
| 评分缺失字段 | **缺失维度从分母剔除**，不按 0 分算，并返回 `coverage` | 避免信息完备性偏差：写得详细的人不该天然比匹配的人分高。详见 ARCHITECTURE.md §6.3 规则 4 |
| 语义维的语义 | `semantic.weight` = 语义分占**最终分**的比例，不是第七个维度 | ARCHITECTURE.md §6.3 规则 1 原文与 §6.2 示例自相矛盾（tech 六维和 1.00 + 0.20 = 1.20）；按示例为准修正文档，公式 `overall = 六维加权分 × (1-w) + 语义分 × w` |
| v1 默认向量库 | `sqlite-vec` / numpy（**阶段 2 才动手**） | ARCHITECTURE.md §7.3 |

> 用户明确说过：**"我们到那个文件夹中开始做"** —— 开发工作在本目录 `D:\AI_resume_screening_self` 进行，
> 不要去参考项目里改代码。

---

## 1. 这个项目是什么

参考开源项目 `liangdabiao/LLM-Agent-Resume` 的架构，**重建**一套智能简历筛选后端。

核心流程（漏斗式）：

```
上传简历 → 解析PDF → LLM抽取元数据 → 嵌入入库
                                        ↓
一句话岗位需求 → LLM解析查询 → 语义召回 → 硬性过滤 → 六维+语义加权评分 → 排序 → Top-N 生成 LLM 报告
```

技术栈：FastAPI + Pydantic v2 + LangChain + 向量库（可插拔）+ OpenAI 兼容接口（LLM 与 Embedding）。

**重建要解决的 5 处结构性缺陷**（详见 ARCHITECTURE.md 附录 A，共 20 条带 `file:line` 证据）：

1. 业务编排耦合在 HTTP 路由里，且 `GET /results/{id}` 有重副作用（每次调用重跑全流程 + N 次 LLM）
2. 两套真源：简历在进程内存字典、向量在 Chroma，重启后不一致
3. 权重 / 召回数 / prompt 全硬编码，且**参考项目自己的 README 与 docs/需求文档.md 权重互相矛盾**
4. 组件是导入期单例，无依赖注入，缺 key 时连 import 都失败
5. 向量库被强加了它不擅长的职责（list 被序列化成 JSON 字符串，导致原生元数据过滤不可用）

**已确定的架构基调**（ARCHITECTURE.md 第 2 节六条原则）：
依赖单向（`domain` 不 import 任何 adapter / fastapi / langchain）· 真源唯一（SQLite 为真源，向量索引与缓存是可重建的派生数据）·
策略外置（权重/prompt 带版本号落库）· 编排集中在 `services/` · 幂等优先（GET 无副作用）· 派生索引可丢弃。

---

## 2. 参考项目（只读参考，不要改它的代码）

| 项 | 值 |
|---|---|
| 路径 | `D:\ai_resume_screening_demo` |
| 仓库 | `https://github.com/liangdabiao/LLM-Agent-Resume.git`（**注意是 `liangdabiao`，不是 `iangdabiao`，后者 404**） |
| 分支 / 提交 | `main`，`7524b74 fix` |
| 测试 | `pytest tests/` → **87 passed**（README 声称 82，套件已增长） |

**常用命令**（务必用 conda 环境里的解释器）：

```bash
# 正确入口（在 D:\ai_resume_screening_demo 下执行）
D:/conda_envs/ai_resume_screening/python.exe -m uvicorn app.main:app --reload --port 8000

# 跑测试
D:/conda_envs/ai_resume_screening/python.exe -m pytest tests/ -q
```

---

## 3. 环境事实（已实测，别再重新踩）

| 项 | 值 |
|---|---|
| conda 环境 | `ai_resume_screening` |
| Python 解释器 | `D:\conda_envs\ai_resume_screening\python.exe`（Python 3.10.22） |
| LLM | DeepSeek 官方 API（OpenAI 兼容），`https://api.deepseek.com` |
| Embedding | **Ollama 本地** `bge-m3`，1024 维 |
| Ollama 地址 | `http://localhost:11434/v1` —— **必须是 `http://`，写成 `https://` 直接 `APIConnectionError`** |
| 向量库 | ChromaDB 本地持久化（`./chroma_db`） |
| 前端 | 内置静态页，根路径 `GET /` 返回 |

**`.env` 关键变量**（参考项目里；密钥为真实值，**任何展示都要打码**）：

- `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` —— 真实配置
- `LLM_API_KEY=${DEEPSEEK_API_KEY}` / `LLM_BASE_URL=${DEEPSEEK_BASE_URL}` / `LLM_MODEL=...`
  —— **代码实际读的是 `LLM_*` 或 `OPENAI_*`**（见 `config/config.py` 的 `_first_env` 回退链），
  所以必须用 `${}` 别名桥接，不要复制密钥
- `EMBEDDING_API_KEY=ollama` / `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL=bge-m3` / `EMBEDDING_DIMENSIONS=1024`
- `VECTOR_DB=chroma` / `CHROMA_PERSIST_DIR=./chroma_db`
- `SERVER_ALLOWED_ORIGINS` / `CACHE_DIR` / `LOG_LEVEL`

> 已实测：`python-dotenv` 的 `${VAR}` 插值**可用**（`dotenv_values` 与 `load_dotenv` 都正常）。
> 另注意：从 stdin 读脚本时 `load_dotenv()` 会因 `find_dotenv` 的 `assert frame.f_back is not None` 报错，
> 传显式路径 `load_dotenv(".env")` 即可。

---

## 4. 已踩过的坑（全部在参考项目上实测得出）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | 启动即 `ModuleNotFoundError: No module named 'app'` | 直接 `python app/main.py` 会把 `app/` 放进 `sys.path`，包内相对导入失效 | 入口是 `uvicorn app.main:app`，不是 `python app/main.py` |
| 2 | import 期 `ValueError: LLM_API_KEY (or OPENAI_API_KEY) environment variable is not set` | `.env` 写的 `DEEPSEEK_*`，代码读 `LLM_*` | 加 `${DEEPSEEK_*}` 别名桥接 |
| 3 | `APIConnectionError` 连 Embedding 失败 | `EMBEDDING_BASE_URL` 写成 `https://localhost:11434/v1`，Ollama 是明文 HTTP | 改 `http://` |
| 4 | `BadRequestError: 400 - invalid input type`（即使已用 HTTP） | langchain `OpenAIEmbeddings` 默认 `check_embedding_ctx_length=True`，会把文本转成 **tiktoken token 数组**发送，Ollama 的 OpenAI 兼容层只接受字符串（裸 SDK 传字符串正常） | 在 `embedding_kwargs` 里加 `"check_embedding_ctx_length": False`（`vector_store.py` 与 `milvus_store.py` 各一处） |
| 5 | 报告凭空说「工作结束时间 2026-09，晚于当前时间」，判定为时间异常 | **不是抽取 bug**——简历原文就是 `2025.08 – 2026.09`，而今天是 2026-10-15，那是上个月。根因是**三个 prompt 模板都不含当前日期**，模型只能用训练数据里的"现在"去推断 | 在 analyzer prompt 注入日期锚点 + 约束 extractor 对"至今"原样返回 + 修掉 filter.py 里写死的 2025 |
| 6 | 经验年限算成天文数字 | `filter.py` 旧逻辑 `x.split('-')[0] if '-' in x else 0`，遇到 `2020.07` 这类无连字符格式起始年算成 0 | 改走已存在但**从未被调用**的 `_parse_year` |

**已实测的修复效果**：

- `2020.07 → 2023.12` 现在算出约 3 年（原来被膨胀到 2025 年）
- `2020-01 → 至今` 保留（动态 6 年）；`2020/06 → 2023/12`、`2020年6月`、`2019.07` 按阈值正确拒绝
- 垃圾数据 `'abc-2020'` 不再抛异常
- analyzer 重跑同一份简历：`晚于`/`尚未到`/`时间异常` 出现次数均为 0，报告改口为「当前为 2026 年 10 月，时间合理」
- Ollama `dimensions` 参数实测：512→512，1024→1024，2048→**1024**（在模型上限处截断）

**已知但未修**（用户已知悉）：

- 经验年限是**年粒度**不是月粒度
- 多段工作经历**时间重叠时仍做累加**而非取并集

---

## 5. 参考项目里我改过的文件（勿回退，改动都是有意的）

```
app/core/analyzer.py     +9   注入当前日期锚点
app/core/extractor.py    +7/-1  work_experience 格式约束 + "至今" 原样返回约束
app/core/filter.py       +19/-5 _filter_by_experience 改走 _parse_year，忽略倒挂数据
app/core/milvus_store.py +3   check_embedding_ctx_length: False
app/core/vector_store.py +3   check_embedding_ctx_length: False
app/main.py              （用户自己的本地改动，纯文案，保留）
```

遗留项：目录下有个 `README.md.placeholder.bak`（原 0 字节占位文件被重命名），待用户决定是否删除。

---

## 6. 设计要点速查

完整内容见 [ARCHITECTURE.md](ARCHITECTURE.md)。几条最容易在设计时走偏的：

- **`domain/` 不许 import** `adapters` / `fastapi` / `langchain` / `chromadb` / `openai`。这条线一划，最难测的业务规则就能纯函数单测。
- **SQLite 是唯一真源**。向量索引、diskcache 缓存、LLM 抽取结果全是**可重建的派生数据**。
  `content_sha256 UNIQUE` 做去重（参考项目每次上传无条件生成新 uuid，同一人会出现两份）。
- **`POST /runs` → 202，`GET /runs/{id}` 只读**。取代参考项目那个有重副作用的 `GET /results/{query_id}`。
- **评分权重外置到 `config/scoring.yaml`，权重和必须 == 1.0，不满足则拒绝启动**。
  **不做静默归一化**——那会让调参的人以为配置生效了（参考项目 `_filter_by_education` 因 `required_level=0` 静默失效是同类毛病）。
- **每维度返回 `DimScore(score, weight, reason)` 三元组**，而不是一个 float。
  参考项目因为 domain 层没把过程信息带出来，只能在 API 层**伪造**单项技能分（`routes.py:71-91`）。
- **结构化过滤走 SQL 下推，不走向量库**（`resume_skills` / `resume_locations` / `resume_education` 建索引）。
  否则"语义排第 11 名的候选人"在进过滤器之前就已被 `top_k=10` 丢弃。
- **`VectorStorePort` 只暴露 `similarity ∈ [0,1]`，彻底删掉 `distance`**。
  参考项目 Milvus 侧做了 `1.0 - distance` 而 Chroma 侧没做，两边语义并不一致，且下游根本没人读这个字段。
- **日期锚点由 `LLMPort` 在 system message 里统一注入**，不要让每个 prompt 模板各写一遍——那正是 §4 第 5 条 bug 的成因。
- **时间写入时一次归一**：统一 `YYYY-MM`；"至今"存 `is_current=1` + `end_ym=NULL`，
  **绝不换算成具体月份**（会把"写入时刻的时钟"固化进数据），需要长度时查询时实时算。
- **LLM 分析只跑 Top-N（默认 10）且并发**。参考项目对**全部**过滤后候选人串行调用，是最贵的一步，而第 11 名之后的报告没人看。

---

## 7. 用户偏好与工作约定

**语言**：始终简体中文。例外保持原样：代码、命令、变量名/函数名、日志输出、报错信息。

**安全**：
- 参考项目的 `.env` 里有**真实 DeepSeek API key**。展示 `.env` 必须打码
  （例如 `sed -E 's/=(.{0,6}).*/\1***/'`，或只列变量名）。新增配置用 `${VAR}` 引用，不要复制密钥。
- `rm -rf` 顶层目录会被内置安全机制拦截。**不要用拆分命令、写脚本、换工具等方式绕过它**——
  碰到就直接告诉用户。

**协作方式**：
- 用户会选择「**硬编码最小修复**」而不是「新增环境变量」这类更"优雅"的方案——**倾向用最小心智负担的改动**，
  提方案时把最小改动版放在第一个选项。
- 用户说过「**修完后，我自己跑一遍验证**」——涉及真实 LLM 调用的端到端验证交给用户自己做，
  助手只做 import 检查、免费的本地 Ollama 调用、定点单测、以及为验证某个具体修复所必需的那一次重跑。
- 用户是二次开发的开发者，熟悉 Python/AI 应用。**不需要从"什么是函数"讲起**，但**遇到新名词、
  新框架必须用通俗语言解释，不要默认他懂**；任何设计选择都要说明优缺点。
- 非平凡实现任务**先出方案再动手**；用户对"整理成文档"这类交付物有明确偏好（要结构化、要能落地）。

**（2026-10-16 用户明确要求的四条，优先级高于上面）**：

1. **讲代码必须先给文件位置**——markdown 链接格式 `[文件名:行号](相对路径#L行号)`，
   不要只贴一段代码却不说它在哪个文件。用户看的是 VSCode，链接可直接点开。
2. **每轮只讲当轮代码，新概念最多 2-3 个**。用户要"彻底搞明白"，但一次塞满会消化不完——
   宁可拆成几轮讲。**回复要短**，长回复本身就是问题。
3. **需要用户拍板的决策只问一次**。小决策助手直接定并说明理由，不要反复追问、不要来回绕圈。
4. **每次开场先给一句当前进度**（"阶段 X 的第 N 轮，下一步 Y"）。

---

## 8. 术语与文件对照

| 参考项目位置 | 本项目对应 | 说明 |
|---|---|---|
| `app/api/routes.py:38-49` | `main.py` 组合根 + `api/deps.py` | 导入期单例 → `Depends` 注入 |
| `app/api/routes.py:52-53` | `db/schema.sql` + `adapters/repo/sqlite_repo.py` | 内存字典 → SQLite |
| `app/api/routes.py:206-266` | `services/screening.py` + `POST /runs` | 路由里的全流程 → 服务层 + 异步任务 |
| `app/api/routes.py:71-91` | `domain/scoring.py::DimScore` | API 层伪造分 → domain 层带出明细 |
| `app/core/retriever.py:54` | `config/scoring.yaml::recall.top_k` | 硬编码 10 → 可配 |
| `app/core/scorer.py:42-49` | `config/scoring.yaml::profiles` | 硬编码权重 → 配置档案 |
| `app/core/metadata_utils.py:14` | `resume_skills` 等索引表 | JSON 字符串 → 结构化表 |
| `app/core/filter.py:52-86`(`_parse_year`, 死代码) | `domain/text.py::parse_year_month` | 唯一实现，写入与过滤共用 |
| `config/config.py` | `config/settings.py` | 手写类 + 死配置 → `pydantic-settings` 启动校验 |
| `tests/conftest.py::FakeEmbeddings` | `tests/contract/` | 确定性假嵌入，CI 友好，**值得保留的模式** |

---

## 变更记录

| 日期 | 说明 |
|---|---|
| 2026-10-15 | 初版。沉淀自对 `liangdabiao/LLM-Agent-Resume` 的完整代码走查、运行验证与 5 处 bug 修复，以及重建方案设计讨论。 |
