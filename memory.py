# memory.py
import os
import sqlite3
import json
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "memory.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT UNIQUE,
            messages TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


def _clean_message(msg) -> dict:
    """把 OpenAI SDK 消息清洗成可安全重放的合法结构"""
    # 1. 统一转 dict
    if hasattr(msg, "model_dump"):
        d = msg.model_dump(exclude_none=True)
    elif hasattr(msg, "to_dict"):
        d = msg.to_dict()
    elif isinstance(msg, dict):
        d = dict(msg)
    else:
        return {"role": "user", "content": str(msg)}

    role = d.get("role")
    out = {"role": role}

    if role == "assistant":
        # content 与 tool_calls 至少要有一个；有 tool_calls 时 content 显式为 None
        tc = d.get("tool_calls")
        fc = d.get("function_call")
        if tc:
            out["tool_calls"] = _clean_tool_calls(tc)
            out["content"] = d.get("content")  # 通常为 None
        elif fc:
            out["function_call"] = fc
            out["content"] = d.get("content")
        else:
            out["content"] = d.get("content") or ""
        if d.get("name"):
            out["name"] = d["name"]
        # refusal 是 openai 的合法字段，若存在则保留
        if d.get("refusal") is not None:
            out["refusal"] = d["refusal"]

    elif role in ("system", "user"):
        out["content"] = d.get("content") if d.get("content") is not None else ""
        if d.get("name"):
            out["name"] = d["name"]

    elif role == "tool":
        out["content"] = d.get("content", "")
        if d.get("tool_call_id"):
            out["tool_call_id"] = d["tool_call_id"]

    elif role == "function":   # 旧式，兼容用
        out["content"] = d.get("content", "")
        if d.get("name"):
            out["name"] = d["name"]

    else:
        # 未知 role：丢弃，返回空则会被外层过滤
        if d.get("content") is not None:
            out["content"] = d["content"]

    return out


def _clean_tool_calls(tool_calls) -> list:
    """只保留 tool_call 的合法字段，去掉 SDK 加的多余键"""
    result = []
    for tc in tool_calls:
        if hasattr(tc, "model_dump"):
            tc = tc.model_dump(exclude_none=True)
        item = {
            "id": tc.get("id"),
            "type": tc.get("type", "function"),
            "function": {
                "name": tc.get("function", {}).get("name"),
                "arguments": tc.get("function", {}).get("arguments", ""),
            },
        }
        result.append(item)
    return result


def save_conversation(messages: list, user_id: str = 'default_user'):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cleaned_messages = []
    for msg in messages:
        m = _clean_message(msg)
        # 【契约修复】system prompt 属于「运行时由当前约束词现构建」的配置，
        # 不属于对话历史，禁止持久化（否则旧约束词会随 load_conversation 复活）。
        if m.get("role") == "system":
            continue
        # 过滤空消息：既没 content 又没 tool_calls / function_call 的丢掉
        if m.get("content") or m.get("tool_calls") or m.get("function_call"):
            cleaned_messages.append(m)

    # 二次校验：清理“孤儿 tool 消息”
    cleaned_messages = _fix_tool_pairing(cleaned_messages)

    messages_json = json.dumps(cleaned_messages, ensure_ascii=False)
    cursor.execute('''
        INSERT OR REPLACE INTO conversations (user_id, messages, updated_at)
        VALUES (?, ?, ?)
    ''', (user_id, messages_json, datetime.now()))
    conn.commit()
    conn.close()


def _fix_tool_pairing(messages: list) -> list:
    """
    保证 assistant.tool_calls 与其对应的 tool 消息成对。
    - 有 tool_calls 的 assistant，后面必须跟足量的 tool（按 tool_call_id 对应）
    - 孤立的 tool 消息（找不到对应 assistant）则丢弃
    """
    # 收集所有 assistant 声明过的 tool_call id
    declared = set()
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                declared.add(tc.get("id"))

    result = []
    for m in messages:
        if m.get("role") == "tool":
            # 孤儿 tool 丢弃
            if m.get("tool_call_id") in declared:
                result.append(m)
        else:
            result.append(m)
    return result

def load_conversation(user_id: str = 'default_user') -> list:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT messages FROM conversations WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        raw = json.loads(row[0])
        # 读出来也清洗一遍，兼容历史脏数据
        cleaned = [_clean_message(m) for m in raw]
        # 【契约修复】丢弃历史里持久化的 system（旧约束词），
        # system 一律由 AgentWithMemory.__init__ 依据当前约束词重建。
        cleaned = [m for m in cleaned
                   if m.get("role") != "system"
                   and (m.get("content") or m.get("tool_calls") or m.get("function_call"))]
        return _fix_tool_pairing(cleaned)

    # 无历史：返回空历史，system 由上层重建（不再返回占位 system）
    return []
