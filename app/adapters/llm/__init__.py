"""LLM 实现。

openai_compat.py   任何说 OpenAI 协议的 provider 都能用
                   （DeepSeek、Ollama、vLLM……换 provider 只改 .env）

将来如果接入 Anthropic / Gemini 原生协议，再加 anthropic_native.py，
services 不用动 —— 它只认 LLMPort。
"""
