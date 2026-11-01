"""SQLite 任务队列：用 tasks 一张表实现 enqueue / dequeue / ack / nack。

JSON 字符串存 payload、ISO 字符串存时间，全程只用一条连接（Database）。
单机场景下这就是最省心的队列；换成 Redis 时只需实现同一个端口。

两个关键语义
------------
1. **可见性锁（visibility timeout）**：dequeue 不是把任务删掉，而是把
   locked_until 推到 N 秒之后。worker 在处理过程中崩溃，锁到期后这条任务
   会重新变成可领取 —— 这是"至少执行一次"的基础。代价是任务可能被重复
   执行，所以业务侧必须幂等（入库按 content_sha256 去重就是为此）。
2. **死信（dead letter）**：重试到上限或遇到永久性错误（文件坏了）时
   状态改成 dead，而不是无限重试把 worker 卡死。dead 的任务留在表里，
   方便人工排查，不会自动删除。
"""

import json
import time
from datetime import datetime, timedelta, timezone

from app.adapters.repo.db import Database, now_iso
from app.ports.task_queue import Task, TaskKind

TIME_FMT = "%Y-%m-%dT%H:%M:%SZ"   # 与 db.now_iso 一致，字典序 == 时间序


def _fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIME_FMT)


class SqliteTaskQueue:
    """基于 Database 的任务队列。

    参数说明
        visibility_timeout_s  任务被领取后的锁定时长。太短 → 还没干完就被别人
                              又领走；太长 → worker 崩溃后任务要等很久才恢复。
        max_attempts          领取次数上限（dequeue 时就 +1），超过则进死信。
        poll_interval_s       没任务时的轮询间隔；SQLite 没有阻塞式出队。
    """

    def __init__(
        self,
        db: Database,
        *,
        visibility_timeout_s: float = 60.0,
        max_attempts: int = 3,
        poll_interval_s: float = 0.1,
    ) -> None:
        if max_attempts < 1:
            raise ValueError(f"max_attempts 至少为 1，实际是 {max_attempts}")
        self._db = db
        self._visibility_timeout_s = visibility_timeout_s
        self._max_attempts = max_attempts
        self._poll_interval_s = poll_interval_s

    # ------------------------------------------------------------
    # 端口实现
    # ------------------------------------------------------------

    def enqueue(self, task: Task) -> None:
        """入队。同一个 id 重复入队是幂等的（什么都不做）。

        ON CONFLICT DO NOTHING 而不是 INSERT OR REPLACE：
        后者会把已被 worker 处理到一半的任务重置回 pending。
        """
        self._db.execute(
            "INSERT INTO tasks (id, kind, payload_json, status, attempts, created_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?) "
            "ON CONFLICT(id) DO NOTHING",
            (
                task.id,
                task.kind.value,                      # 存 "parse_resume"，不是 "TaskKind.PARSE_RESUME"
                json.dumps(task.params, ensure_ascii=False),
                task.attempts,
                now_iso(),
            ),
        )

    def dequeue(self, timeout: float) -> Task | None:
        """取一条任务并上锁；最多等 timeout 秒，超时返回 None。

        先尝试一次再去判断 deadline，所以 timeout=0 表示"只试一次"。
        """
        deadline = time.monotonic() + max(0.0, timeout)

        while True:
            task = self._claim_one()
            if task is not None:
                return task
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(self._poll_interval_s, remaining))

    def ack(self, task_id: str) -> None:
        """确认完成：从队列删除。业务成功之后才调用。"""
        self._db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    def nack(self, task_id: str, retry: bool, *, error: str | None = None) -> None:
        """失败回退。

        retry=True  且没超过上限 → 解锁，重新可领取（这条消息会再被 dequeue 到）
        retry=False 或已超过上限 → status='dead'，进死信，不再被领取
        """
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT attempts FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:          # 已经被 ack 或本来就不存在：什么都不用做
                return

            if retry and row["attempts"] < self._max_attempts:
                conn.execute(
                    "UPDATE tasks SET locked_until = NULL, last_error = ? WHERE id = ?",
                    (error, task_id),
                )
            else:
                conn.execute(
                    "UPDATE tasks SET status = 'dead', locked_until = NULL, "
                    "last_error = ? WHERE id = ?",
                    (error, task_id),
                )

    # ------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------

    def _claim_one(self) -> Task | None:
        """原子地领取一条可见任务。

        子查询挑一条（pending 且锁已过期），UPDATE 同一事务里把它锁住。
        attempts 在**领取时**就 +1，而不是在 nack 时：
        worker 领取后直接崩溃（没机会 nack）也必须算一次尝试，
        否则这条任务会无限重试。
        """
        now = datetime.now(timezone.utc)
        locked_until = _fmt(now + timedelta(seconds=self._visibility_timeout_s))

        with self._db.transaction() as conn:
            row = conn.execute(
                "UPDATE tasks SET attempts = attempts + 1, locked_until = ? "
                "WHERE id = ("
                "  SELECT id FROM tasks "
                "  WHERE status = 'pending' "
                "    AND (locked_until IS NULL OR locked_until < ?) "
                "  ORDER BY created_at, rowid LIMIT 1"
                ") "
                "RETURNING id, kind, payload_json, attempts",
                (locked_until, now_iso()),
            ).fetchone()

        if row is None:
            return None

        return Task(
            id=row["id"],
            kind=TaskKind(row["kind"]),
            payload=tuple(json.loads(row["payload_json"]).items()),
            attempts=row["attempts"],
        )


__all__ = ["SqliteTaskQueue"]
