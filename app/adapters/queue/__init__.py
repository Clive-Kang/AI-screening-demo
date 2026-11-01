"""任务队列适配器。

现在是 SQLite 一张表；将来换成 Redis / RabbitMQ 只需换这个包里的实现，
worker 与 services 一行不用改（它们只认 TaskQueuePort）。
"""

from app.adapters.queue.sqlite_queue import SqliteTaskQueue

__all__ = ["SqliteTaskQueue"]
