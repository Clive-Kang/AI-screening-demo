"""智能简历筛选系统（重建版）。

分层结构见 ARCHITECTURE.md §3：

    api  ->  services  ->  domain
     |          |           ^
     +----------+--> ports --+
                     ^
               adapters +

依赖方向严格单向。本文件的唯一职责是让 app/ 成为一个 Python 包。
"""

__version__ = "0.1.0"
