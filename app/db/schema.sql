-- ============================================================
-- 智能简历筛选系统 · SQLite 表结构
--
-- 对应 ARCHITECTURE.md §4.2。全部 DDL 都是 IF NOT EXISTS，
-- 可以在每次启动时无条件执行一遍（幂等）。
--
-- 三条贯穿全表的约定：
--   1. 主键与时间都是 TEXT。时间统一 ISO-8601（2026-10-18T09:30:00Z），
--      不做时区转换 —— 全系统只用 UTC，展示层再转本地。
--   2. 日期到月存 'YYYY-MM'（start_ym / end_ym），"至今"存 is_current=1
--      且 end_ym 为 NULL。绝不把"至今"换算成具体月份，
--      那会把"写入时刻的时钟"永久固化进数据（ARCHITECTURE.md §4.4）。
--   3. 外键全部 ON DELETE CASCADE：删一份简历，它的技能、经历、
--      向量记录、候选结果必须一起走，否则会留下指向空处的脏行。
-- ============================================================

-- ===== 简历本体（真源）=====
--对应 `Resume` domain 模型，仓储 ResumeRepository 操作这个表。
--`IF NOT EXISTS`：**幂等建表**。如果表已经存在，不报错，直接跳过。
--字段定义语法:格式：`字段名 类型 [约束] [DEFAULT 默认值]`  PRIMARY KEY:主键
CREATE TABLE IF NOT EXISTS resumes (
    id              TEXT PRIMARY KEY,           -- uuid4，由仓储层生成
    filename        TEXT NOT NULL,              -- 原始文件名，仅用于展示与排查
    content_sha256  TEXT NOT NULL UNIQUE,       -- 文件内容哈希，去重靠它
    blob_path       TEXT,                       -- 原文件落盘路径；NULL 表示不保存原文件
    raw_text        TEXT,                       -- 解析出的纯文本；异步解析完成前为 NULL
    char_count      INTEGER,                    -- raw_text 的字符数，方便一眼看出解析是否正常
    status          TEXT NOT NULL,              -- queued|parsed|extracted|indexed|failed
    error           TEXT,                       -- status=failed 时的原因
    created_at      TEXT NOT NULL
);
--CREATE INDEX IF NOT EXISTS 索引名 ON 表名(字段1, 字段2...);

CREATE INDEX IF NOT EXISTS idx_resumes_status ON resumes(status);
--`idx_resumes_status`：worker 筛选待解析简历，按状态过滤高频查询
CREATE INDEX IF NOT EXISTS idx_resumes_created ON resumes(created_at DESC);
--`idx_resumes_created`：按时间倒序查最近上传的简历，方便排查。

-- ===== 抽取出的结构化元数据（整份 JSON）=====
-- 为什么不把 name/email/phone 也拆成列？
--   它们从来不单独查询，只在"读出一份简历"时整体使用。
--   拆成列只是把 JSON 的键换成列名，却要多写一堆映射代码。
-- 真正需要 WHERE 的字段（技能/地点/学历/经历）在下面拆表。
CREATE TABLE IF NOT EXISTS resume_metadata (
    resume_id   TEXT PRIMARY KEY REFERENCES resumes(id) ON DELETE CASCADE,
    data_json   TEXT NOT NULL,                  -- ResumeMetadata 的 JSON 序列化
    extractor_prompt_version TEXT NOT NULL,     -- 哪版 prompt 抽的，换模型后可比对
    updated_at  TEXT NOT NULL
);

-- ===== 需要 SQL 下推的结构化表 =====
-- 存在的理由（ARCHITECTURE.md §7.4）：如果只在向量库里按 top_k 取，
-- "语义排第 11 名但技能完全匹配"的人根本进不了候选池。
-- 这些表的 WHERE 在 SQL 里先缩小范围，再交给向量与评分。

CREATE TABLE IF NOT EXISTS resume_skills (
    resume_id   TEXT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    skill_raw   TEXT NOT NULL,                  -- 原写法：FastAPI
    skill_norm  TEXT NOT NULL,                  -- 归一化：fastapi（比较只用这个）
    PRIMARY KEY (resume_id, skill_norm)
);
--索引`idx_skills_norm`：快速查找拥有该技能的所有简历。
CREATE INDEX IF NOT EXISTS idx_skills_norm ON resume_skills(skill_norm);

CREATE TABLE IF NOT EXISTS resume_locations (
    resume_id     TEXT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    location_norm TEXT NOT NULL,                -- 归一化城市名：北京
    PRIMARY KEY (resume_id, location_norm)
);
CREATE INDEX IF NOT EXISTS idx_locations_norm ON resume_locations(location_norm);

CREATE TABLE IF NOT EXISTS resume_education (
    resume_id    TEXT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    institution  TEXT,                          -- 学校名
    major        TEXT,                          -- 专业名
    degree_raw   TEXT NOT NULL,                 -- 原文写法：本科 / Bachelor
    degree_level INTEGER NOT NULL,              -- 0=未知 1=大专 2=本科 3=硕士 4=博士
    start_ym     TEXT,                          -- 'YYYY-MM'
    end_ym       TEXT,
    is_current   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_education_resume ON resume_education(resume_id);
CREATE INDEX IF NOT EXISTS idx_education_level ON resume_education(degree_level);

CREATE TABLE IF NOT EXISTS resume_experience (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  --AUTOINCREMENT:插入新记录自动生成递增整数 id。
    resume_id   TEXT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    --如果主表 resumes 里面某条简历被删除，所有子表中 `resume_id` 匹配的行**自动删除**
    company     TEXT,
    title       TEXT,
    start_ym    TEXT,                           -- 'YYYY-MM'
    end_ym      TEXT,                           -- 在职中为 NULL
    is_current  INTEGER NOT NULL DEFAULT 0,     -- 1 = "至今"
    -- 单段经历的月数。冗余存一份是为了让 SQL 能直接 SUM，
    -- 但多段重叠的归并发生在 domain（text.merge_experience_months），
    -- 因为"两段同时进行"不是一个 SQL 聚合能表达清楚的。
    months      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_experience_resume ON resume_experience(resume_id);

-- ===== 需求解析结果 =====
-- 单独成表而不是塞进 runs，是因为同一条需求可能被用不同 profile 跑多次，
-- 解析结果可以复用，不必每次都调 LLM。
CREATE TABLE IF NOT EXISTS queries (
    id            TEXT PRIMARY KEY,
    query_text    TEXT NOT NULL,                -- HR 原话
    parsed_json   TEXT NOT NULL,                -- JobRequirement 的 JSON
    parser_prompt_version TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- ===== 筛选任务 =====
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    -- 直接存需求原文，而不是 query_id 外键。
    -- 原因：建 run 时还没有解析结果（解析是异步 worker 的第一步），
    -- 用外键就会逼着 API 层先解析再落库；而幂等键与展示又只需要原文。
    -- queries 表保留作"解析结果缓存"，有需要时按 query_text 反查，不是必需前置。
    query_text    TEXT NOT NULL,                -- HR 原话
    profile       TEXT NOT NULL,                -- tech / sales / strict
    config_snapshot_json TEXT NOT NULL,         -- 本次生效的完整配置，用于事后复算
    idempotency_key TEXT UNIQUE,                -- 幂等key sha256(query, profile, corpus_version)
    status        TEXT NOT NULL,                -- queued|running|succeeded|partial|failed|cancelled
    stage         TEXT,                         -- recall|filter|score|analyze|done
    progress_json TEXT,                         -- {"recall": 120, "filtered": 34}
    recall_n   INTEGER NOT NULL DEFAULT 0,
    filtered_n INTEGER NOT NULL DEFAULT 0,
    scored_n   INTEGER NOT NULL DEFAULT 0,      -- 各阶段统计数字
    analyzed_n INTEGER NOT NULL DEFAULT 0,

    llm_calls  INTEGER NOT NULL DEFAULT 0,      -- 成本审计
    llm_tokens INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    created_at    TEXT NOT NULL
);
--`idx_runs_idem`：快速按幂等 key 查重，重复请求直接返回已有任务
CREATE INDEX IF NOT EXISTS idx_runs_idem ON runs(idempotency_key);
--`idx_runs_status`：前端任务列表，按状态 + 创建时间倒序查询
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status, created_at DESC);

-- ===== 筛选结果 =====
-- 被淘汰的人也要有一行（hard_filter_passed=0 + reject_reason），
-- 否则"这个人为什么没出现"永远回答不了（ARCHITECTURE.md §6.4）。
CREATE TABLE IF NOT EXISTS results (
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    resume_id   TEXT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    rank        INTEGER NOT NULL,
    overall_score REAL,                         -- 允许 NULL：六维全缺失且无语义分
    semantic_score REAL,                        -- 归一化到 [0,1]
    dim_scores_json TEXT NOT NULL,              -- 六个 DimScore 的明细
    hard_filter_passed INTEGER NOT NULL,
    reject_reason TEXT,
    analysis_text  TEXT,                        -- LLM 报告；只有 Top-N 有值
    analysis_prompt_version TEXT,
    PRIMARY KEY (run_id, resume_id)
);
CREATE INDEX IF NOT EXISTS idx_results_rank ON results(run_id, rank);

-- ===== 语料版本（幂等键的第三个因子）=====
-- 每次简历库变化（新增/删除/重新抽取）就 +1。
-- 它的意义：同样的 query + 同样的 profile，但简历库变了 → 必须重算。
CREATE TABLE IF NOT EXISTS corpus_versions (
    version      INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_count INTEGER NOT NULL,
    content_hash TEXT NOT NULL,                 -- 全部 content_sha256 的聚合哈希
    created_at   TEXT NOT NULL
);

-- ===== 任务队列（v1 用 SQLite 当队列）=====
-- 一行 = 一条待办。worker 取任务时写 locked_until，
-- 崩溃后锁超时自动可再取（可见性锁，见 ports/task_queue.py）。
CREATE TABLE IF NOT EXISTS tasks (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,                 -- parse_resume|screen_run
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|dead
    attempts     INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,                          -- NULL = 可领取
    last_error   TEXT,
    created_at   TEXT NOT NULL
);
-- 取任务的查询形如：
--   WHERE status='pending' AND (locked_until IS NULL OR locked_until < :now)
--   ORDER BY created_at LIMIT 1
CREATE INDEX IF NOT EXISTS idx_tasks_ready ON tasks(status, locked_until, created_at);
