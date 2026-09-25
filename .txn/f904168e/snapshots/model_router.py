# model_router.py
"""
多模型路由 + 项目契约层。

设计目标：
  - 支持「本地模型（Ollama）」和「API 模型（DeepSeek）」在同一套 Agent 里切换；
  - Agent 只认 model 名字，由本模块决定用哪个 client / base_url；
  - 契约层（PROJECT_CONTRACT）会被注入到两个模型的 system prompt 里，
    保证「两个模型负责同一项目的不同模块时，接口能对齐」。

升级预留：
  - get_contract() 现在返回内置常量字符串（方案 A：契约写进 prompt）。
    未来要升级到「方案 B（独立 contract.md / 向量检索）」时，
    只需改这一个函数：从文件读取 / 从向量库检索，其余调用方无需改动。
"""

import os
from openai import OpenAI

# ============================================================
# 1. 模型注册表
# ============================================================
# 每个模型登记：用哪个 client、什么类型、给前端展示的名字。
# 新增模型只要往这里加一项即可。

_LOCAL_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://192.168.166.128:11434/v1")
_DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")

MODEL_REGISTRY = {
    # ---- 本地模型（Ollama），先加一个 ----
    "qwen2.5:7b": {
        "client": OpenAI(api_key="ollama", base_url=_LOCAL_BASE_URL),
        "type": "local",
        "label": "本地 Qwen2.5-7B (Ollama)",
        "vision": False,
    },
    # ---- API 模型（DeepSeek）----
    "deepseek-chat": {
        "client": OpenAI(api_key=_DEEPSEEK_KEY, base_url="https://api.deepseek.com"),
        "type": "api",
        "label": "DeepSeek-chat (API)",
        "vision": False,
    },
    # ---- API 模型（DeepSeek，多模态识图）----
    "deepseek-flash": {
        "client": OpenAI(api_key=_DEEPSEEK_KEY, base_url="https://api.deepseek.com"),
        "type": "api",
        "label": "DeepSeek-flash 识图 (API)",
        "vision": True,
    },
}

# 前端下拉框展示用的模型列表
AVAILABLE_MODELS = [
    {"id": mid, "label": cfg["label"], "type": cfg["type"], "vision": cfg.get("vision", False)}
    for mid, cfg in MODEL_REGISTRY.items()
]


def get_client(model_name: str) -> OpenAI:
    """按模型名取对应的 OpenAI 兼容 client"""
    if model_name not in MODEL_REGISTRY:
        raise ValueError(f"未知模型: {model_name}，可用: {list(MODEL_REGISTRY.keys())}")
    return MODEL_REGISTRY[model_name]["client"]


def get_model_type(model_name: str) -> str:
    """返回模型类型：local / api"""
    return MODEL_REGISTRY.get(model_name, {}).get("type", "unknown")


def supports_vision(model_name: str) -> bool:
    """返回该模型是否支持识图（vision）。未知模型按 False 处理。"""
    return bool(MODEL_REGISTRY.get(model_name, {}).get("vision", False))


# ============================================================
# 2. 项目契约层（接口契合的关键）
# ============================================================
# 方案 A：契约作为常量直接注入 system prompt。
# 想要改契约，改这段文字即可，两个模型都会看到同一份。
#
# 升级到方案 B 时，把 get_contract() 改成：
#     def get_contract(): 
#         return open("contract.md", encoding="utf-8").read()
# 其余代码（agent 里调用 get_contract()）完全不用动。

PROJECT_CONTRACT = """## 项目接口契约（所有模块共同遵守）
本项目的不同模块由不同模型/会话分别开发，为了让模块能对接，必须遵守以下约定：

1. **命名约定**：变量、函数、接口字段统一用 snake_case（Python）/ camelCase（JS）。
2. **数据结构**：所有跨模块传递的数据必须先在契约里定义字段名和类型，不得随意改字段名。
3. **接口签名**：函数/HTTP 接口一旦确定，参数顺序、名称、返回结构不得单方面修改；
   如需变更，必须在契约里记录变更，并说明影响范围。
4. **错误约定**：统一返回结构 `{"ok": bool, "data": ..., "error": str|None}`。
5. **契约优先**：当你不确定另一模块的接口时，先写明"这里依赖 X 接口，约定为 …"，
   而不是自己编一个不一样的字段名。

> 当你（模型）产出任何会被其他模块调用的函数、类、HTTP 接口、数据结构时，
> 必须在回答里显式给出其**完整签名/字段定义**，方便其他模块对齐。
"""


def get_contract() -> str:
    """
    返回当前项目契约文本。
    （方案 A：内置常量；未来升级方案 B 时只改这里）
    """
    return PROJECT_CONTRACT
