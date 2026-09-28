# billing.py
"""
平台用户计费体系（v1：token 余额制）

职责：
  - 签发/管理平台 API Key（sk-xxx），每个 Key 绑定一个 user_id 与 token 余额；
  - 提供「按增量扣费 / 充值 / 查询余额」能力，供 api.py 在 /chat、/chat/stream 中调用；
  - 记录扣费流水（./data/billing/usage_log.json，仅保留最近 2000 条）。

设计约定（契约）：
  - 所有对外函数返回统一结构 {"ok": bool, "data": ..., "error": str|None}；
  - 余额单位 = token（int），与 agent 上报的 usage.total 同源；
  - 扣费按「增量」进行：调用方传入的 tokens 是本次新增消耗
    （api.py 负责「全轮累计 → 增量」换算，见 api.py 的 bill_event）；
  - api_key 明文落盘于本地文件（个人自部署可接受），生产环境建议加密存储。

数据文件（BILLING_DIR，默认 ./data/billing/）：
  accounts.json  : [{api_key, user_id, balance, total_used, created_at, note, disabled}]
  usage_log.json : [{ts, api_key(掩码), user_id, model, tokens, balance_after, source}]
"""

import os
import json
import time
import secrets
import threading

_BILLING_DIR = os.environ.get("BILLING_DIR", "./data/billing")
_ACCOUNTS_FILE = os.path.join(_BILLING_DIR, "accounts.json")
_LOG_FILE = os.path.join(_BILLING_DIR, "usage_log.json")
_LOG_CAP = 2000

_lock = threading.Lock()
_accounts = {}          # api_key -> account dict
_loaded = False


def _load():
    global _loaded
    if _loaded:
        return
    try:
        if os.path.exists(_ACCOUNTS_FILE):
            with open(_ACCOUNTS_FILE, encoding="utf-8") as f:
                for acc in json.load(f):
                    _accounts[acc["api_key"]] = acc
    except Exception as e:
        print(f"⚠️ 载入计费账户失败: {e}")
    _loaded = True


def _save():
    try:
        os.makedirs(_BILLING_DIR, exist_ok=True)
        tmp = _ACCOUNTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(_accounts.values()), f, ensure_ascii=False, indent=2)
        os.replace(tmp, _ACCOUNTS_FILE)
    except Exception as e:
        print(f"⚠️ 保存计费账户失败: {e}")


def _append_log(entry: dict):
    try:
        os.makedirs(_BILLING_DIR, exist_ok=True)
        logs = []
        if os.path.exists(_LOG_FILE):
            with open(_LOG_FILE, encoding="utf-8") as f:
                logs = json.load(f)
        logs.append(entry)
        logs = logs[-_LOG_CAP:]
        tmp = _LOG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(logs, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _LOG_FILE)
    except Exception as e:
        print(f"⚠️ 计费流水写入失败: {e}")


def _mask(key: str) -> str:
    return (key[:8] + "***" + key[-4:]) if len(key) >= 12 else "***"


def _public_view(acc: dict) -> dict:
    """对外展示视图：不含完整 api_key（仅掩码）"""
    return {
        "api_key_masked": _mask(acc["api_key"]),
        "user_id": acc["user_id"],
        "balance": acc["balance"],
        "total_used": acc["total_used"],
        "created_at": acc["created_at"],
        "note": acc.get("note", ""),
        "disabled": acc.get("disabled", False),
    }


# ============ 对外契约函数 ============

def create_key(user_id: str, init_tokens: int = 0, note: str = "") -> dict:
    """
    签发新平台 Key。
    契约返回 data = {"api_key": str(完整明文，仅签发时可见一次), ..._public_view 字段}
    """
    uid = (user_id or "").strip()
    if not uid:
        return {"ok": False, "data": None, "error": "user_id 不能为空"}
    try:
        init_tokens = int(init_tokens)
    except (TypeError, ValueError):
        return {"ok": False, "data": None, "error": "init_tokens 必须是整数"}
    if init_tokens < 0:
        return {"ok": False, "data": None, "error": "init_tokens 不能为负"}
    _load()
    api_key = "sk-" + secrets.token_hex(16)
    with _lock:
        acc = {
            "api_key": api_key,
            "user_id": uid,
            "balance": init_tokens,
            "total_used": 0,
            "created_at": int(time.time()),
            "note": (note or "").strip(),
            "disabled": False,
        }
        _accounts[api_key] = acc
        _save()
    data = {"api_key": api_key}
    data.update(_public_view(acc))
    return {"ok": True, "data": data, "error": None}


def list_keys() -> dict:
    """管理员：列出全部 Key（含完整 api_key，仅管理端使用）。
    契约返回 data = {"keys": [完整 account dict...]}"""
    _load()
    with _lock:
        keys = [dict(a) for a in _accounts.values()]
    keys.sort(key=lambda a: a.get("created_at", 0))
    return {"ok": True, "data": {"keys": keys}, "error": None}


def resolve_key(api_key: str) -> dict:
    """校验 Key（内部用）。data = 完整 account；无效/禁用 → ok=False"""
    key = (api_key or "").strip()
    if not key:
        return {"ok": False, "data": None, "error": "api_key 为空"}
    _load()
    with _lock:
        acc = _accounts.get(key)
        if acc is None:
            return {"ok": False, "data": None, "error": "api_key 无效"}
        if acc.get("disabled"):
            return {"ok": False, "data": None, "error": "api_key 已禁用"}
        return {"ok": True, "data": dict(acc), "error": None}


def get_balance(api_key: str) -> dict:
    """用户自助查询余额（对外视图，不含完整 key）。data = _public_view"""
    res = resolve_key(api_key)
    if not res.get("ok"):
        return res
    return {"ok": True, "data": _public_view(res["data"]), "error": None}


def recharge(api_key: str, tokens: int) -> dict:
    """充值（tokens > 0）。data = 充值后的 _public_view"""
    try:
        tokens = int(tokens)
    except (TypeError, ValueError):
        return {"ok": False, "data": None, "error": "tokens 必须是整数"}
    if tokens <= 0:
        return {"ok": False, "data": None, "error": "tokens 必须为正数"}
    key = (api_key or "").strip()
    _load()
    with _lock:
        acc = _accounts.get(key)
        if acc is None:
            return {"ok": False, "data": None, "error": "api_key 无效"}
        acc["balance"] += tokens
        _save()
        _append_log({"ts": int(time.time()), "api_key": _mask(key), "user_id": acc["user_id"],
                     "model": "", "tokens": tokens, "balance_after": acc["balance"],
                     "source": "recharge"})
        return {"ok": True, "data": _public_view(acc), "error": None}


def deduct(api_key: str, tokens: int, model: str = "", source: str = "chat") -> dict:
    """
    按增量扣费。
    - tokens <= 0：视作成功（无需扣费）；
    - 余额不足：ok=False（不扣、不透支），data={"balance": 当前余额}。
    """
    try:
        tokens = int(tokens)
    except (TypeError, ValueError):
        return {"ok": False, "data": None, "error": "tokens 必须是整数"}
    if tokens <= 0:
        return {"ok": True, "data": None, "error": None}
    key = (api_key or "").strip()
    _load()
    with _lock:
        acc = _accounts.get(key)
        if acc is None:
            return {"ok": False, "data": None, "error": "api_key 无效"}
        if acc.get("disabled"):
            return {"ok": False, "data": {"balance": acc["balance"]}, "error": "api_key 已禁用"}
        if acc["balance"] < tokens:
            return {"ok": False, "data": {"balance": acc["balance"]},
                    "error": f"余额不足：需要 {tokens}，剩余 {acc['balance']}"}
        acc["balance"] -= tokens
        acc["total_used"] += tokens
        _save()
        _append_log({"ts": int(time.time()), "api_key": _mask(key), "user_id": acc["user_id"],
                     "model": model, "tokens": tokens, "balance_after": acc["balance"],
                     "source": source})
        return {"ok": True, "data": {"balance": acc["balance"]}, "error": None}


def refund(api_key: str, tokens: int, reason: str = "") -> dict:
    """退款（预留：本方案按增量扣费通常用不到，断点回滚场景可用）"""
    try:
        tokens = int(tokens)
    except (TypeError, ValueError):
        return {"ok": False, "data": None, "error": "tokens 必须是整数"}
    if tokens <= 0:
        return {"ok": True, "data": None, "error": None}
    key = (api_key or "").strip()
    _load()
    with _lock:
        acc = _accounts.get(key)
        if acc is None:
            return {"ok": False, "data": None, "error": "api_key 无效"}
        acc["balance"] += tokens
        _save()
        _append_log({"ts": int(time.time()), "api_key": _mask(key), "user_id": acc["user_id"],
                     "model": "", "tokens": tokens, "balance_after": acc["balance"],
                     "source": f"refund:{reason}"})
        return {"ok": True, "data": _public_view(acc), "error": None}


def disable_key(api_key: str, disabled: bool = True) -> dict:
    """启用/禁用 Key。data = _public_view"""
    key = (api_key or "").strip()
    _load()
    with _lock:
        acc = _accounts.get(key)
        if acc is None:
            return {"ok": False, "data": None, "error": "api_key 无效"}
        acc["disabled"] = bool(disabled)
        _save()
        return {"ok": True, "data": _public_view(acc), "error": None}


def list_logs(limit: int = 50) -> dict:
    """管理端：最近扣费/充值流水（最新在前）。data = {"logs": [...]}"""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    logs = []
    try:
        if os.path.exists(_LOG_FILE):
            with open(_LOG_FILE, encoding="utf-8") as f:
                logs = json.load(f)
    except Exception as e:
        print(f"⚠️ 读取计费流水失败: {e}")
    return {"ok": True, "data": {"logs": list(reversed(logs))[:limit]}, "error": None}
