# model_router.py
"""
多模型路由 + 项目契约层 + 运行时模型注册 + 约束词管理。

设计目标：
  - 支持「本地模型（Ollama）」和「API 模型（OpenAI 兼容）」在同一套 Agent 里切换；
  - Agent 只认 model 名字，由本模块决定用哪个 client / base_url；
  - 契约层（PROJECT_CONTRACT）会被注入到两个模型的 system prompt 里，
    保证「两个模型负责同一项目的不同模块时，接口能对齐」。
  - 【新增】支持前端用 API key 快速接入任意 OpenAI 兼容模型（运行时注册）；
  - 【新增】支持前端修改「约束词」（仅 system prompt 的业务约束部分，不含项目契约）。

契约优先说明：
  本模块对外暴露的函数签名/字段定义如下，其它模块（api.py / agent_with_memory.py）依赖这些约定，
  不得单方面修改：

  函数签名：
    get_client(model_name: str) -> OpenAI
    get_model_type(model_name: str) -> str            # 返回 "local" | "api" | "unknown"
    supports_vision(model_name: str) -> bool
    get_contract() -> str                             # 项目契约文本（只读，前端不可改）
    get_constraint(model: str = "") -> str            # 当前生效的「约束词」（可被前端覆盖）
    set_constraint(model: str = "", text: str = "") -> dict   # {"ok":bool,"data":{...},"error":str|None}
    constraint_signature(model: str = "") -> tuple    # 【方案B'】约束词签名，(st_mtime_ns, st_size) 或 (None, None)
    register_model(spec: dict) -> dict                # {"ok":bool,"data":{...},"error":str|None}
    list_models() -> list[dict]                       # 前端下拉框用

  数据结构：
    模型项（list_models 返回元素）字段：
      {"id": str, "label": str, "type": "local"|"api", "vision": bool, "custom": bool}
    注册入参 spec 字段：
      {
        "id": str,          # 必填，模型名（调用 API 时传的 model）
        "label": str,       # 可选，展示名，缺省=id
        "api_key": str,     # 必填，API Key
        "base_url": str,    # 必填，OpenAI 兼容 base_url，如 https://api.deepseek.com
        "vision": bool,     # 可选，默认 false
      }
"""

import os
import re
import json
import threading
from openai import OpenAI

# ============================================================
# 1. 模型注册表
# ============================================================
# 每个模型登记：用哪个 client、什么类型、给前端展示的名字。
# 内置模型 + 运行时注册模型统一存放在 MODEL_REGISTRY。

_LOCAL_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://192.168.166.128:11434/v1")
_DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY")

# 运行时注册的模型持久化文件（重启后仍可用）
_RUNTIME_MODELS_FILE = os.environ.get("RUNTIME_MODELS_FILE", "./data/runtime_models.json")


def _make_local_registry() -> dict:
    return {
        # ---- 本地模型（Ollama），先加一个 ----
        "qwen2.5:7b": {
            "client": OpenAI(api_key="ollama", base_url=_LOCAL_BASE_URL),
            "type": "local",
            "label": "本地 Qwen2.5-7B (Ollama)",
            "vision": False,
            "custom": False,
        },
        # ---- API 模型（DeepSeek）----
        "deepseek-chat": {
            "client": OpenAI(api_key=_DEEPSEEK_KEY, base_url="https://api.deepseek.com"),
            "type": "api",
            "label": "DeepSeek-chat (API)",
            "vision": False,
            "custom": False,
        },
        # ---- API 模型（DeepSeek，多模态识图）----
        "deepseek-flash": {
            "client": OpenAI(api_key=_DEEPSEEK_KEY, base_url="https://api.deepseek.com"),
            "type": "api",
            "label": "DeepSeek-flash 识图 (API)",
            "vision": True,
            "custom": False,
        },
    }


MODEL_REGISTRY = _make_local_registry()

# 运行时注册模型只需存「配置」，client 可懒加载/重建
_RUNTIME_CONFIGS = {}   # id -> {"api_key","base_url","label","vision"}
_registry_lock = threading.Lock()


def _load_runtime_models():
    """启动时载入持久化的运行时模型"""
    global _RUNTIME_CONFIGS
    try:
        if os.path.exists(_RUNTIME_MODELS_FILE):
            with open(_RUNTIME_MODELS_FILE, encoding="utf-8") as f:
                _RUNTIME_CONFIGS = json.load(f)
            for mid, cfg in _RUNTIME_CONFIGS.items():
                MODEL_REGISTRY[mid] = {
                    "client": OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"]),
                    "type": "api",
                    "label": cfg.get("label", mid),
                    "vision": bool(cfg.get("vision", False)),
                    "custom": True,
                }
            print(f"📦 已载入 {len(_RUNTIME_CONFIGS)} 个运行时注册模型")
    except Exception as e:
        print(f"⚠️ 载入运行时模型失败: {e}")


def _save_runtime_models():
    """持久化运行时模型（仅存可序列化的配置）"""
    try:
        os.makedirs(os.path.dirname(_RUNTIME_MODELS_FILE) or ".", exist_ok=True)
        with open(_RUNTIME_MODELS_FILE, "w", encoding="utf-8") as f:
            json.dump(_RUNTIME_CONFIGS, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存运行时模型失败: {e}")


def register_model(spec: dict) -> dict:
    """
    运行时注册一个 OpenAI 兼容模型。

    入参 spec: {"id":str, "api_key":str, "base_url":str, "label"?:str, "vision"?:bool}
    返回契约结构：{"ok":bool, "data": {模型项} | None, "error": str|None}

    唯一性：id 即平台模型名，必须与平台对齐、天然唯一。
            - 与内置模型重名 → 拒绝（内置模型不可覆盖）。
            - 与已有自定义模型重名 → 拒绝，error="已有该模型：{mid}，如需修改请先删除后重新接入"。
            即任何同名接入都不会静默覆盖，必须显式删除旧模型后再接入。
    """
    try:
        mid = (spec.get("id") or "").strip()
        api_key = (spec.get("api_key") or "").strip()
        base_url = (spec.get("base_url") or "").strip()
        if not mid:
            return {"ok": False, "data": None, "error": "id 不能为空"}
        if not api_key:
            return {"ok": False, "data": None, "error": "api_key 不能为空"}
        if not base_url:
            return {"ok": False, "data": None, "error": "base_url 不能为空"}
        # 防止覆盖内置模型
        if mid in MODEL_REGISTRY and not MODEL_REGISTRY[mid].get("custom"):
            return {"ok": False, "data": None, "error": f"内置模型 {mid} 不可覆盖"}
        # 【契约变更】模型 ID 与平台模型名一一对应，天然唯一：
        # 已存在同名模型（含自定义）时直接拒绝，不再静默覆盖。
        if mid in MODEL_REGISTRY:
            return {"ok": False, "data": None, "error": f"已有该模型：{mid}，如需修改请先删除后重新接入"}

        label = (spec.get("label") or mid).strip()
        vision = bool(spec.get("vision", False))

        with _registry_lock:
            _RUNTIME_CONFIGS[mid] = {
                "api_key": api_key, "base_url": base_url,
                "label": label, "vision": vision,
            }
            MODEL_REGISTRY[mid] = {
                "client": OpenAI(api_key=api_key, base_url=base_url),
                "type": "api",
                "label": label,
                "vision": vision,
                "custom": True,
            }
            _save_runtime_models()

        item = {"id": mid, "label": label, "type": "api", "vision": vision, "custom": True}
        return {"ok": True, "data": item, "error": None}
    except Exception as e:
        return {"ok": False, "data": None, "error": str(e)}


def list_models() -> list:
    """返回前端下拉框展示用的模型列表（字段契约见文件头）"""
    return [
        {"id": mid, "label": cfg["label"], "type": cfg["type"],
         "vision": cfg.get("vision", False), "custom": cfg.get("custom", False)}
        for mid, cfg in MODEL_REGISTRY.items()
    ]


def get_client(model_name: str) -> OpenAI:
    """按模型名取对应的 OpenAI 兼容 client"""
    if model_name not in MODEL_REGISTRY:
        raise ValueError(f"未知模型: {model_name}，可用: {list(MODEL_REGISTRY.keys())}")
    return MODEL_REGISTRY[model_name]["client"]


def get_model_type(model_name: str) -> str:
    """返回模型类型：local / api / unknown"""
    return MODEL_REGISTRY.get(model_name, {}).get("type", "unknown")


def supports_vision(model_name: str) -> bool:
    """返回该模型是否支持识图（vision）。未知模型按 False 处理。"""
    return bool(MODEL_REGISTRY.get(model_name, {}).get("vision", False))


# ============================================================
# 2. 项目契约层（接口契合的关键）
# ============================================================
# 方案 A：契约作为常量直接注入 system prompt。
# ⚠️ 契约是「只读」的，前端不可修改，只有约束词（DEFAULT_CONSTRAINT）可被前端覆盖。
#
# 升级到方案 B 时，把 get_contract() 改成从文件/向量库读取即可，调用方无需改动。

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
    返回当前项目契约文本（只读，前端不可改）。
    （方案 A：内置常量；未来升级方案 B 时只改这里）
    """
    return PROJECT_CONTRACT


# ============================================================
# 3. 约束词层（前端可改，但仅限约束词）
# ============================================================
# 约束词 = system prompt 里的「业务规则/人设/工具说明」部分。
# 前端可覆盖它，但项目契约（get_contract）永远拼在后面且不可被覆盖。
#
# 持久化：方案A —— 一模型一文件。
#   目录 ./data/constraints/，文件名 "{model_id}.txt"。
#   文件不存在 / 内容为空 → 该模型用默认约束词。
#   项目契约（get_contract）永远拼在约束词之后且不可被覆盖。
# 兼容：环境变量 CONSTRAINT_FILE 仍可指定「默认文件」（旧全局文件），
#       仅当调用方传 model="" 时使用它，保证旧调用不崩。

_CONSTRAINT_DIR = os.environ.get("CONSTRAINT_DIR", "./data/constraints")
# 旧全局文件的回退（model 为空时）
_LEGACY_CONSTRAINT_FILE = os.environ.get("CONSTRAINT_FILE", "./data/constraint.txt")

DEFAULT_CONSTRAINT = """你是一个智能助手，可以根据用户需求调用合适的工具来解决问题。

## 可用工具
- get_weather: 查询指定城市的天气
- get_current_time: 获取当前日期和时间
- calculate: 执行数学计算
- web_search: 联网搜索最新信息
- read_file: 读取文件内容
- write_file: 写入或修改文件
- append_to_file: 追加内容到文件
- run_command: 执行终端命令（受白名单和黑名单限制）

## 运维助手能力
当用户问服务器状态时，你可以调用 run_command 执行以下命令：
- 磁盘：df -h、du -sh
- 内存：free -h
- 进程：ps aux、top -bn1
- 网络：ss -tlnp、netstat -tlnp
- Docker：docker ps、docker logs、docker images
- 服务：systemctl status xxx
- 日志：journalctl -u xxx -n 50
- 文件占用：lsof

## 重要规则
1. 当用户要求修改文件时，直接调用 write_file 工具
2. 当用户要求读取文件时，直接调用 read_file 工具
3. 如果需要多个步骤才能完成任务，请连续调用工具，直到任务完成,不要每做一步就停下来等用户追问。
4. 历史对话片段仅供参考，优先根据当前问题回答
"""

_constraint_lock = threading.Lock()


def _constraint_path(model: str) -> str:
    """返回某模型约束词的存储路径。model 为空 → 回退到旧全局文件。"""
    m = (model or "").strip()
    if not m:
        return _LEGACY_CONSTRAINT_FILE
    # 安全：避免路径穿越（model 名里的斜杠等）
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", m)
    return os.path.join(_CONSTRAINT_DIR, safe + ".txt")


def get_constraint(model: str = "") -> str:
    """返回指定模型当前生效的约束词。文件存在则读文件，否则用默认值。"""
    path = _constraint_path(model)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if text.strip():
                return text
    except Exception as e:
        print(f"⚠️ 读取约束词失败({model}): {e}")
    return DEFAULT_CONSTRAINT


def constraint_signature(model: str = "") -> tuple:
    """
    【方案B'·契约新增】返回约束词文件的「签名」，用于判断约束词是否已变更。

    语义：
      返回 (st_mtime_ns, st_size)。
      - 文件存在：返回其 (纳秒级修改时间, 字节大小)——**只 stat 元数据，不读取文件内容**。
      - 文件不存在（该模型用默认约束词）：返回 (None, None)——默认值视作恒定签名。

    用途：
      AgentWithMemory 每轮请求前调用本函数比对签名，签名变化时重建 system prompt，
      从而让「前端修改约束词」在无需重启、无需清缓存的情况下即时生效。

    成本：单次 os.stat，纳秒~微秒级，不涉及文件内容读取。
    失败兜底：任何异常均返回 (None, None)，绝不会抛错影响主流程。
    """
    try:
        st = os.stat(_constraint_path(model))
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        # 文件不存在或无权限 → 视为默认约束词（恒定签名）
        return (None, None)
    except Exception:
        return (None, None)


def set_constraint(model: str = "", text: str = "") -> dict:
    """
    覆盖指定模型的约束词并持久化。
    契约返回：{"ok":bool, "data":{"constraint":str, "is_default":bool}|None, "error":str|None}

    - text 为空字符串：视为「恢复默认」（删除该模型的文件）。
    """
    try:
        with _constraint_lock:
            path = _constraint_path(model)
            if not (text or "").strip():
                if os.path.exists(path):
                    os.remove(path)
                return {"ok": True, "data": {"constraint": DEFAULT_CONSTRAINT, "is_default": True}, "error": None}
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return {"ok": True, "data": {"constraint": text, "is_default": False}, "error": None}
    except Exception as e:
        return {"ok": False, "data": None, "error": str(e)}


def get_constraint_meta(model: str = "") -> dict:
    """约束词元信息：{"constraint":str, "is_default":bool, "default":str, "model":str}"""
    cur = get_constraint(model)
    return {
        "model": model,
        "constraint": cur,
        "is_default": (cur == DEFAULT_CONSTRAINT),
        "default": DEFAULT_CONSTRAINT,
    }


def delete_model(model_id: str) -> dict:
    """
    删除一个「运行时注册」的模型，并清理其专属约束词文件。
    契约返回：{"ok":bool, "data":None, "error":str|None}
    - 内置模型（custom=False）禁止删除。
    - 模型不存在 → ok=False。
    """
    mid = (model_id or "").strip()
    if not mid:
        return {"ok": False, "data": None, "error": "model_id 不能为空"}
    with _registry_lock:
        cfg = MODEL_REGISTRY.get(mid)
        if cfg is None:
            return {"ok": False, "data": None, "error": f"模型 {mid} 不存在"}
        if not cfg.get("custom"):
            return {"ok": False, "data": None, "error": f"内置模型 {mid} 不可删除"}
        MODEL_REGISTRY.pop(mid, None)
        _RUNTIME_CONFIGS.pop(mid, None)
        _save_runtime_models()
    # 清理该模型专属约束词
    try:
        cpath = _constraint_path(mid)
        if os.path.exists(cpath):
            os.remove(cpath)
    except Exception as e:
        print(f"⚠️ 清理约束词文件失败({mid}): {e}")
    return {"ok": True, "data": None, "error": None}


# 启动时载入运行时模型
_load_runtime_models()

# 兼容旧调用方式：AVAILABLE_MODELS 保持为「模型项列表」快照的构造函数。
# ⚠️ 注意：这是启动时的快照。运行时新增模型后，请调用 list_models() 获取最新列表。
AVAILABLE_MODELS = list_models()


# ============================================================
# 【契约新增·余额查询配套】模型配置读取
# ============================================================
def get_model_config(model_id: str):
    """
    返回模型配置（不含 client 对象），供 balance_service 等模块读取 api_key/base_url。

    契约：返回 {"id": str, "api_key": str, "base_url": str,
                "type": "local"|"api", "custom": bool} | None
      - 模型不存在 → None
      - local 模型：api_key / base_url 返回空串（无上游）
      - custom=True：优先读 _RUNTIME_CONFIGS（注册时的原始配置）
      - custom=False（内置 api 模型）：从 client 对象读 api_key / base_url

    ⚠️ 仅限服务端内部使用（返回明文 api_key），任何 HTTP 响应不得透传 api_key 字段。
    纯新增函数，不影响本模块其他既有签名。
    """
    mid = (model_id or "").strip()
    cfg = MODEL_REGISTRY.get(mid)
    if cfg is None:
        return None
    if cfg.get("type") == "local":
        return {"id": mid, "api_key": "", "base_url": "",
                "type": "local", "custom": bool(cfg.get("custom", False))}
    api_key, base_url = "", ""
    if cfg.get("custom") and mid in _RUNTIME_CONFIGS:
        rc = _RUNTIME_CONFIGS[mid]
        api_key = rc.get("api_key", "")
        base_url = rc.get("base_url", "")
    else:
        client = cfg.get("client")
        try:
            api_key = getattr(client, "api_key", "") or ""
            base_url = str(getattr(client, "base_url", "") or "")
        except Exception:
            api_key, base_url = "", ""
    return {"id": mid, "api_key": api_key, "base_url": base_url,
            "type": cfg.get("type", "api"), "custom": bool(cfg.get("custom", False))}
