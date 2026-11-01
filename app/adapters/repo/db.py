"""SQLite 连接管理：建库、开 PRAGMA、开事务。

为什么单独一个类，而不是各仓储自己 connect？
    有三个 PRAGMA 必须**每条连接**都设一次，忘一个就会静默出错：

        foreign_keys = ON     忘了 → 删简历不级联，留下孤儿行，不报错
        journal_mode = WAL    忘了 → 读操作被写操作阻塞
        busy_timeout = 5000   忘了 → 并发写时立刻抛 "database is locked"

    把它们收在一处，就不会有新写的仓储忘记设置。

关于线程安全
    sqlite3 的连接默认不允许跨线程使用。FastAPI 用线程池跑同步路由，
    所以这里 check_same_thread=False 关掉检查，并用一把锁串行化访问。
    单机、写操作只来自后台任务，一把锁完全够；代价是读也被串行化了。
    真到读成为瓶颈时，再改成"每线程一条连接"（WAL 模式支持一写多读）。
"""

import sqlite3    #Python 内置 SQLite 驱动
import threading  #线程锁，保证并发安全
from contextlib import contextmanager  #装饰器，把函数变成`with`上下文管理器（`transaction`事务）
from datetime import datetime, timezone  #生成 UTC 标准 ISO 时间
from pathlib import Path          #文件路径管理，读取 schema.sql 建表脚本

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"
#`resolve()`拿到绝对路径；`parents[2]`向上跳两级目录，定位到项目根目录下`db/schema.sql`

def now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串，秒级精度。

    为什么不用 time.time() 存浮点数？
        可读性。数据库里出现 1760000000.0 时，排查问题还得心算一次。
    为什么用 UTC？
        全系统只有一种时间基准，展示层再转本地时区。
    """
    #datetime.now(timezone.utc) —— 取UTC时间，不是本地时间
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    #.strftime(...) —— 格式化成字符串

class Database:
#这个类代表:单个 sqlite 数据库文件，持有唯一一条数据库连接。
#使用示例写在文档字符串，分为初始化建表、事务批量写入、只读查询。
    """一个 SQLite 文件 + 一条连接。

    用法：

        db = Database("app.db")
        db.initialize()                       # 建表（幂等）

        with db.transaction() as conn:        # 一组写操作
            conn.execute(...)
            conn.execute(...)                 # 要么全成，要么全回滚

        rows = db.query("SELECT ...")         # 只读
    """

    def __init__(self, path: str | Path) -> None:#构造函数（创建连接，一次性设置全部 PRAGMA）
        # ":memory:" 用于测试：不进磁盘，进程结束即消失
        self.path = str(path)
        self._lock = threading.RLock()  #可重入锁，同一个线程可以多次获取锁
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,   # 关闭跨线程检测，配合锁手动保证安全
            isolation_level=None,      # 关掉 sqlite3 的隐式事务，改由我们显式控制
        )
        # 让 SELECT 的结果能按列名取值：row["status"] 而不是 row[3]
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")

    # ---- 建表 ----

    def initialize(self) -> None:
        """执行 schema.sql。DDL 全部 IF NOT EXISTS，可重复调用。"""
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        with self._lock:
            self._conn.executescript(schema) #executescript()：一次性执行多条 SQL 语句

    # ---- 读 ----

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # ---- 写 ----

    @contextmanager  #让这个函数支持`with`语法
    def transaction(self):
        """显式事务。

        isolation_level=None 意味着 sqlite3 不会自动 BEGIN，
        这里手动开、手动提交/回滚。

        为什么必须用事务而不是一条条自动提交？
            一次上传要写 resumes + resume_metadata + 4 张拆表，
            中途失败若已提交一半，库里就是"有简历但没技能"的半成品，
            而且状态字段没法自洽。事务保证要么全有、要么全无。
        """
        #业务场景：上传简历，需要一次性写入`resumes`、`resume_metadata`、
        #技能、地点、教育、经历多张表。如果中间 LLM 报错中断，事务会全部回滚，不会出现半残数据。

        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        """单条写操作（自带一次事务）。批量写请用 transaction()。"""
        with self.transaction() as conn:
            conn.execute(sql, params)

    def close(self) -> None:
        with self._lock:
            self._conn.close()
