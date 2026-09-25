# observability.py
import logging
import json
import time
import uuid
from datetime import datetime
from typing import Optional

# ============ 日志配置 ============
logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(handler)

# ============ 全局统计 ============
STATS = {
    "total_requests": 0,
    "total_tokens": 0,
    "total_tool_calls": 0,
    "tool_calls": {},
    "tool_durations": {},
    "errors": 0,
}


def log_event(event_type: str, request_id: Optional[str] = None, **kwargs):
    """记录结构化日志"""
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "event": event_type,
    }
    if request_id:
        log_entry["request_id"] = request_id
    log_entry.update(kwargs)
    logger.info(json.dumps(log_entry, ensure_ascii=False))


def new_request_id() -> str:
    """生成唯一的请求 ID"""
    return str(uuid.uuid4())[:8]


def log_llm_call(model: str, usage, request_id: str = None):
    """记录 LLM 调用和 Token 消耗"""
    if usage:
        prompt_tokens = getattr(usage, "prompt_tokens", 0)
        completion_tokens = getattr(usage, "completion_tokens", 0)
        total_tokens = getattr(usage, "total_tokens", 0)
        STATS["total_tokens"] += total_tokens
        log_event(
            "llm_call",
            request_id=request_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )


def track_tool_call(tool_name: str, arguments: dict, duration_ms: float, success: bool, error: str = None, request_id: str = None):
    """记录工具调用"""
    STATS["total_tool_calls"] += 1
    STATS["tool_calls"][tool_name] = STATS["tool_calls"].get(tool_name, 0) + 1
    if tool_name not in STATS["tool_durations"]:
        STATS["tool_durations"][tool_name] = []
    STATS["tool_durations"][tool_name].append(duration_ms)
    if not success:
        STATS["errors"] += 1
    log_event(
        "tool_call",
        request_id=request_id,
        tool=tool_name,
        args=arguments,
        duration_ms=round(duration_ms, 2),
        success=success,
        error=error,
    )


def log_agent_transition(from_agent: str, to_agent: str, request_id: str = None):
    """记录 Agent 切换"""
    log_event("agent_transition", request_id=request_id, from_agent=from_agent, to_agent=to_agent)


def log_request_start(user_input: str, request_id: str):
    """记录请求开始"""
    STATS["total_requests"] += 1
    log_event("request_start", request_id=request_id, input=user_input[:100])


def log_request_end(request_id: str, duration_ms: float, success: bool = True):
    """记录请求结束"""
    log_event("request_end", request_id=request_id, duration_ms=round(duration_ms, 2), success=success)


def get_stats() -> dict:
    """获取统计摘要"""
    avg_durations = {}
    for tool, durations in STATS["tool_durations"].items():
        if durations:
            avg_durations[tool] = round(sum(durations) / len(durations), 2)
    return {
        "total_requests": STATS["total_requests"],
        "total_tokens": STATS["total_tokens"],
        "total_tool_calls": STATS["total_tool_calls"],
        "tool_calls": STATS["tool_calls"],
        "avg_durations_ms": avg_durations,
        "errors": STATS["errors"],
    }
