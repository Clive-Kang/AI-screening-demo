"""任务队列端口：异步后台任务调度，把长任务移出 HTTP。

一条简历要解析 PDF + 调一次 LLM，10 份串行要 2-5 分钟（ARCHITECTURE.md §5.5）。
放在 POST 里同步跑，用户浏览器早就超时了。

这套接口刻意长得像消息队列（RabbitMQ / SQS）的四个动作，
但 v1 的实现只是 SQLite 里一张表：

    enqueue   INSERT INTO tasks（同 id 幂等）
    dequeue   UPDATE ... SET locked_until = now + 60, attempts += 1
              WHERE id = (SELECT ...) RETURNING *
    ack       DELETE FROM tasks
    nack      retry → 解锁、可重新领取；永久失败或超过上限 → status='dead'

换成 Redis / RabbitMQ 时，services 一行不改。
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class TaskKind(str, Enum):
    """任务的两种类型，对应两条流水线。"""

    PARSE_RESUME = "parse_resume"   # 流水线 A：解析 → 抽取 → 嵌入 → 索引
    SCREEN_RUN = "screen_run"       # 流水线 B：召回 → 过滤 → 评分 → 分析


@dataclass(frozen=True)
class Task:
    """队列里的一条任务。

    payload 用 tuple of pairs 而不是 dict，理由同 Run.progress：
    frozen dataclass 里的 dict 照样能被改，传进队列后可能被谁悄悄改掉。
    取值用 `dict(task.payload)["resume_id"]`，或者调用 task.params 属性。
    """

    id: str
    kind: TaskKind
    payload: tuple[tuple[str, str], ...] = () #`payload`：任务的业务参数
    attempts: int = 0 #重试次数，记录这个任务已经被 worker 尝试处理多少次，用来做最大重试次数控制

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Task.id 不能为空")
        if self.attempts < 0:
            raise ValueError(f"attempts 不能为负，实际是 {self.attempts}")

    @property
    def params(self) -> dict[str, str]:
        """payload 的只读用法：每次返回一份新 dict，改了也不影响本体。"""
        return dict(self.payload)


@runtime_checkable
class TaskQueuePort(Protocol):
    """队列的四个基本动作。"""

    def enqueue(self, task: Task) -> None:
        """入队。同 id 重复入队应当幂等（不产生两条）。"""
        ...

    def dequeue(self, timeout: float) -> Task | None:
        """取一条并**加锁**，没有任务时最多等 timeout 秒。

        返回 None 表示超时 —— 不是错误，worker 会继续下一轮。

        为什么要加锁而不是直接取走？
        任务被取走后、ack 之前，如果 worker 崩溃，任务不能凭空消失。
        加锁（SQLite 里是 locked_until 时间戳）让它超时后能被重新取到。
        """
        ...

    def ack(self, task_id: str) -> None:
        """确认完成，从队列删除。

        必须在业务逻辑**成功之后**调用。先 ack 再干活，
        崩溃时这条任务就永久丢了。
        """
        ...

    def nack(self, task_id: str, retry: bool, *, error: str | None = None) -> None:
        """失败回退，error 会记进任务的 last_error 供排查。

        retry=True   重新可领取，用于网络抖动这类可恢复错误
        retry=False  直接进死信，用于"文件坏了"这类重试一万次也没用的错误

        尝试次数由 dequeue 负责 +1（含 worker 领取后崩溃、没机会 nack 的情况），
        nack 只决定"是否还有下一次机会"。超过实现侧的上限同样进死信。

        这个布尔值就是区分「瞬态失败」和「永久失败」的地方。
        参考项目没有队列，一次解析失败就等于用户白传。
        """
        ...
