"""端口层：只声明「要什么能力」，不写「怎么实现」。

架构约束（ARCHITECTURE.md §2 原则 P1、§7.1）
------------------------------------------------
本包及其所有子模块 **禁止** import：

  1. 任何 adapters 的具体实现（sqlite / chroma / openai …）
  2. 任何第三方框架

只允许：Python 标准库 + app.domain。

为什么要有这一层
------------------------------------------------
参考项目的组件是「导入期单例」——import config 的那一刻就建好了 LLM 客户端，
没配 API key 连 import 都失败，测试时也没法换成假实现。

端口把这件事反过来：业务代码（services）只依赖这里的 Protocol，
main.py 启动时再决定塞进哪个实现。于是：

  生产  → adapters/llm/openai_compat.py（真调 DeepSeek）
  测试  → tests 里的 FakeLLM（返回写死的 JSON，零网络）

两者对 services 完全等价，因为它只认 Protocol 的方法签名。
"""
