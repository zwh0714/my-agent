# balance_service.py
"""
上游平台余额查询服务（按已注册模型的 api_key 查询该账户在上游的剩余额度）。

支持范围（P0，按用户确认不含中转站）：
  - deepseek    : GET {base}/user/balance     （官方文档已核实）
  - siliconflow : GET {base}/v1/user/info     （官方文档已核实；balance 字段语义实现后以实测校准）
  - 其他/本地    : supported=false，不发探测请求（不猜端点、不产生 401 噪音）

契约（函数）：
  detect_provider(base_url: str) -> str                      # "deepseek"|"siliconflow"|"unknown"
  fetch_balance(provider, api_key, base_url, timeout=5.0) -> {"ok","data":BalanceInfo|None,"error"}
  get_model_balance(model_id, force_refresh=False) -> 同上   # TTL 缓存 60s
  list_all_balances(force_refresh=False) -> {"ok","data":{"balances":[BalanceInfo...],"checked_at":int},"error":None}

BalanceInfo 统一字段（跨模块，不得改名）：
  {"model_id": str, "provider": str, "supported": bool, "ok": bool,
   "unit": "currency"|"tokens"|"none", "currency": "CNY"|"USD"|"",
   "balance": float|None, "stale": bool, "extra": dict,
   "checked_at": int, "error": str|None}

安全：任何返回不含 api_key；全部为只读 GET，不产生上游费用。
依赖：model_router.get_model_config（本方案新增，见 model_router.py 末尾）。
"""

import time
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import httpx

from model_router import list_models, get_model_config

_CACHE_TTL = 60.0
_cache = {}             # model_id -> {"data": BalanceInfo, "ts": float}
_cache_lock = threading.Lock()


def _to_float(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _new_info(model_id: str, provider: str) -> dict:
    return {
        "model_id": model_id,
        "provider": provider,
        "group": "",
        "supported": False,
        "ok": False,
        "unit": "none",
        "currency": "",
        "balance": None,
        "stale": False,
        "extra": {},
        "checked_at": 0,
        "error": None,
    }


def detect_provider(base_url: str) -> str:
    """域名嗅探。返回 "deepseek" | "siliconflow" | "unknown" """
    try:
        host = (urlparse((base_url or "").strip()).hostname or "").lower()
    except Exception:
        return "unknown"
    if "deepseek" in host:
        return "deepseek"
    if "siliconflow" in host:
        return "siliconflow"
    return "unknown"


def _group_of(base_url: str, api_key: str) -> str:
    """【契约新增】同一上游账户的分组标识 = sha1(规范化base_url|api_key) 前8位。
    前端据此去重（多个模型共用一个 key 只显示一次余额，其余标「共用↑」）。
    base_url 先规范化（忽略结尾 / 与 /v1 后缀、大小写），避免同账户不同写法分组不一致。
    哈希脱敏，不泄露 key 本身。"""
    b = (base_url or "").strip().lower().rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    if not (b or api_key):
        return ""
    return hashlib.sha1((b + "|" + (api_key or "")).encode("utf-8")).hexdigest()[:8]


def _strip_v1(base: str) -> str:
    b = (base or "").strip().rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b


def _auth_err_status(status: int) -> str:
    suffix = "（api_key 无效或无权限）" if status in (401, 403) else ""
    return f"HTTP {status}{suffix}"


def _fetch_deepseek(api_key: str, base_url: str, timeout: float) -> dict:
    """GET {base}/user/balance → balance_infos[0].total_balance（官方文档已核实）"""
    url = _strip_v1(base_url) + "/user/balance"
    with httpx.Client(timeout=timeout) as cli:
        resp = cli.get(url, headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code != 200:
        return {"ok": False, "data": None, "error": _auth_err_status(resp.status_code)}
    body = resp.json()
    infos = body.get("balance_infos") or []
    if not infos:
        return {"ok": False, "data": None, "error": "响应缺少 balance_infos 字段"}
    top = infos[0]
    data = _new_info("", "deepseek")
    data.update({
        "supported": True, "ok": True,
        "unit": "currency", "currency": top.get("currency", ""),
        "balance": _to_float(top.get("total_balance")),
        "extra": {"is_available": body.get("is_available"),
                  "granted_balance": top.get("granted_balance"),
                  "topped_up_balance": top.get("topped_up_balance")},
        "checked_at": int(time.time()), "error": None,
    })
    return {"ok": True, "data": data, "error": None}


def _fetch_siliconflow(api_key: str, base_url: str, timeout: float) -> dict:
    """GET {base}/v1/user/info → data.balance（官方文档已核实）"""
    b = (base_url or "").strip().rstrip("/")
    url = (b + "/user/info") if b.endswith("/v1") else (b + "/v1/user/info")
    with httpx.Client(timeout=timeout) as cli:
        resp = cli.get(url, headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code != 200:
        return {"ok": False, "data": None, "error": _auth_err_status(resp.status_code)}
    body = resp.json()
    d = body.get("data") or {}
    if "balance" not in d:
        return {"ok": False, "data": None, "error": "响应缺少 balance 字段"}
    data = _new_info("", "siliconflow")
    data.update({
        "supported": True, "ok": True,
        "unit": "currency", "currency": "CNY",
        "balance": _to_float(d.get("balance")),
        "extra": {"chargeBalance": d.get("chargeBalance"), "totalBalance": d.get("totalBalance")},
        "checked_at": int(time.time()), "error": None,
    })
    return {"ok": True, "data": data, "error": None}


_ADAPTERS = {"deepseek": _fetch_deepseek, "siliconflow": _fetch_siliconflow}


def fetch_balance(provider: str, api_key: str, base_url: str, timeout: float = 5.0) -> dict:
    """实时查询单个上游余额。data=BalanceInfo（model_id 字段留空，由上层填充）"""
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        info = _new_info("", provider)
        info["error"] = ("无法识别上游平台（不支持余额查询）" if provider == "unknown"
                         else "该平台无公开余额查询接口")
        return {"ok": False, "data": info, "error": info["error"]}
    if not api_key:
        info = _new_info("", provider)
        info["error"] = "该模型未配置 api_key"
        return {"ok": False, "data": info, "error": info["error"]}
    try:
        return adapter(api_key, base_url, timeout)
    except httpx.TimeoutException:
        return {"ok": False, "data": None, "error": "上游查询超时"}
    except Exception as e:
        return {"ok": False, "data": None, "error": f"上游查询失败: {e}"}


def _query_model(mid: str, cfg: dict, timeout: float = 5.0) -> dict:
    """查询单个模型（含 local/unknown 分支），返回 BalanceInfo"""
    if cfg.get("type") == "local":
        info = _new_info(mid, "local")
        info["error"] = "本地模型无额度概念"
        return info
    base = (cfg.get("base_url") or "").strip()
    provider = detect_provider(base)
    grp = _group_of(base, cfg.get("api_key") or "")
    info = _new_info(mid, provider)
    info["group"] = grp
    if provider == "unknown":
        info["error"] = "无法识别上游平台（不支持余额查询）"
        return info
    res = fetch_balance(provider, cfg.get("api_key") or "", base, timeout)
    if res.get("ok") and res.get("data"):
        info.update(res["data"])
        info["model_id"] = mid
        info["group"] = grp  # 适配器返回的 group 为空串，用本地计算值覆盖
        return info
    info["error"] = res.get("error") or "查询失败"
    return info


def get_model_balance(model_id: str, force_refresh: bool = False) -> dict:
    """带 TTL 缓存（60s）的单模型查询。data=BalanceInfo；模型不存在 → ok=False"""
    mid = (model_id or "").strip()
    cfg = get_model_config(mid)
    if cfg is None:
        return {"ok": False, "data": None, "error": f"模型 {mid} 不存在"}
    now = time.time()
    if not force_refresh:
        with _cache_lock:
            hit = _cache.get(mid)
            if hit and now - hit["ts"] < _CACHE_TTL:
                data = dict(hit["data"])
                data["stale"] = False
                return {"ok": True, "data": data, "error": None}
    info = _query_model(mid, cfg)
    if info.get("ok"):
        with _cache_lock:
            _cache[mid] = {"data": dict(info), "ts": time.time()}
    else:
        # 失败：有上次成功值则降级展示并标记 stale
        with _cache_lock:
            hit = _cache.get(mid)
            if hit:
                info = dict(hit["data"])
                info["stale"] = True
                info["error"] = info["error"] or "本次刷新失败，展示上次结果"
    return {"ok": True, "data": info, "error": None}


def list_all_balances(force_refresh: bool = False, max_workers: int = 4) -> dict:
    """并发查询全部模型（local 模型直接返回不支持标记；失败降级 stale）"""
    models = list_models()
    results = {}
    todo = []
    now = time.time()

    for m in models:
        mid = m["id"]
        cfg = get_model_config(mid)
        if cfg is None:
            continue
        if not force_refresh:
            with _cache_lock:
                hit = _cache.get(mid)
                if hit and now - hit["ts"] < _CACHE_TTL:
                    d = dict(hit["data"])
                    d["stale"] = False
                    results[mid] = d
                    continue
        if cfg.get("type") == "local":
            info = _new_info(mid, "local")
            info["error"] = "本地模型无额度概念"
            results[mid] = info
            continue
        todo.append((mid, cfg))

    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futs = {pool.submit(_query_model, mid, cfg): mid for mid, cfg in todo}
            for fut, mid in futs.items():
                try:
                    info = fut.result()
                except Exception as e:
                    info = _new_info(mid, "unknown")
                    info["error"] = f"查询异常: {e}"
                if info.get("ok"):
                    with _cache_lock:
                        _cache[mid] = {"data": dict(info), "ts": time.time()}
                else:
                    with _cache_lock:
                        hit = _cache.get(mid)
                        if hit:
                            info = dict(hit["data"])
                            info["stale"] = True
                results[mid] = info

    balances = [results[m["id"]] for m in models if m["id"] in results]
    return {"ok": True, "data": {"balances": balances, "checked_at": int(time.time())}, "error": None}
