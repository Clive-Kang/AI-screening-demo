"""适配器层：ports 的具体实现。

与 domain / ports 相反，这一层**允许** import 任何东西——
sqlite3、PyMuPDF、openai SDK、chromadb。它的职责就是把外部世界
翻译成端口定义的样子。

但反过来有一条约束：**只有 main.py / worker.py（组合根）可以 import adapters**。
services 一旦直接 import 某个 adapter，接口隔离就白做了，
测试时也没法注入假实现。
"""
