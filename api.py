# api.py
import os
import json
import threading
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from agent_with_memory import AgentWithMemory, DEFAULT_MODEL
from typing import List, Optional
from model_router import (
    list_models,
    register_model,
    delete_model,
    get_constraint_meta,
    set_constraint,
    AVAILABLE_MODELS,
)
from observability import get_stats
from knowledge_agent import ask_knowledge
from balance_service import get_model_balance, list_all_balances
import billing
import uvicorn

# ============ 数据目录 ============
DATA_DIR = os.environ.get("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)

# ============ 服务端口（可被环境变量 PORT 覆盖，默认 8000） ============
PORT = int(os.environ.get("PORT", "8000"))

# ============ 并发配置 ============
MAX_CONCURRENCY = 8

# ============ 管理员令牌（可选）：设置后 /billing 管理接口必须带 X-Admin-Token 头 ============
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

app = FastAPI(title="Agent API", description="你的智能助手 Agent")

executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENCY, thread_name_prefix="agent")
semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

# 挂载静态文件
app.mount("/static", StaticFiles(directory="static"), name="static")


# ============ 请求/响应模型 ============
class ChatRequest(BaseModel):
    user_id: str = "default_user"
    message: str
    model: str = DEFAULT_MODEL          # ← 前端指定模型
    # 方案A：图片以 base64 dataURL 数组随请求发送，图文同一次请求。
    # 契约：images: List[str]，元素形如 "data:image/png;base64,...."；缺省为空列表。
    images: List[str] = []
    # 契约新增：单次请求内 LLM↔工具自主循环上限。可选，默认 25，建议范围 1~40。
    max_iterations: int = 25
    # 契约新增：断点续跑控制。resume=True 从暂停态继续；abandon=True 放弃并结算。
    resume: bool = False
    abandon: bool = False


class ChatResponse(BaseModel):
    response: str


class ReportRequest(BaseModel):
    topic: str


class KnowledgeRequest(BaseModel):
    question: str


# 契约新增：运行时注册模型入参
class RegisterModelRequest(BaseModel):
    id: str
    api_key: str
    base_url: str
    label: str = ""
    vision: bool = False


# 契约新增：修改约束词入参
class ConstraintRequest(BaseModel):
    # 契约新增：目标模型 id。缺省 "" 时回退全局（兼容旧调用），前端一律显式传。
    model: str = ""
    constraint: str = ""


# 【计费体系】管理入参
class CreateKeyRequest(BaseModel):
    user_id: str
    init_tokens: int = 0
    note: str = ""


class RechargeRequest(BaseModel):
    api_key: str
    tokens: int


class KeyOpRequest(BaseModel):
    api_key: str
    disabled: bool = True


# ============ 鉴权辅助 ============

def _admin_guard(x_admin_token: Optional[str]):
    """管理员鉴权：设置 ADMIN_TOKEN 环境变量后，管理接口必须带 X-Admin-Token 头；
    未设置（个人自部署默认）→ 不校验。"""
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="需要管理员令牌（X-Admin-Token）")


def _parse_bearer(authorization: Optional[str]) -> str:
    if not authorization:
        return ""
    key = authorization.strip()
    if key.lower().startswith("bearer "):
        key = key[7:].strip()
    return key


def _resolve_billing(authorization: Optional[str]):
    """
    计费鉴权钩子（/chat、/chat/stream 专用）。
    返回 (account|None, raw_key, err|None)：
      - 未带 Authorization 头 → (None, "", None)：匿名模式，行为与旧版完全一致；
      - 带了头但 Key 无效/禁用 → (None, key, err)：调用方应拒绝请求（401）；
      - 有效 → (account, key, None)。account 为完整账户 dict（含 user_id/balance）。
    """
    raw = _parse_bearer(authorization)
    if not raw:
        return None, "", None
    res = billing.resolve_key(raw)
    if not res.get("ok"):
        return None, raw, res
    return res["data"], raw, None


def _billing_precheck(authorization: Optional[str]):
    """返回 (account|None, raw_key)；无效 Key 抛 401，余额<=0 抛 402。"""
    account, raw_key, err = _resolve_billing(authorization)
    if err is not None:
        raise HTTPException(status_code=401, detail=err.get("error", "api_key 无效"))
    if account and account.get("balance", 0) <= 0:
        raise HTTPException(status_code=402,
                            detail=f"余额不足（剩余 {account.get('balance', 0)} tokens），请充值")
    return account, raw_key


# ============ Agent 实例管理（每个「用户::模型」一个，线程安全 + LRU 上限） ============
MAX_AGENTS = 128

_agents: "OrderedDict[str, AgentWithMemory]" = OrderedDict()
_agents_lock = threading.Lock()


def _agent_key(user_id: str, model: str) -> str:
    """
    记忆隔离的关键：把「用户 + 模型」拼成唯一 key。
    这样同一个用户在两个模型下的记忆互不污染，
    但通过共享的项目契约（system prompt 里的 get_contract）保证接口契合。
    """
    return f"{user_id}::{model}"


def get_agent(user_id: str, model: str) -> AgentWithMemory:
    """获取或创建「用户::模型」对应的 Agent 实例（线程安全，超出上限淘汰最久未用）"""
    key = _agent_key(user_id, model)
    with _agents_lock:
        agent = _agents.get(key)
        if agent is None:
            agent = AgentWithMemory(user_id=key, model=model)
            _agents[key] = agent
        _agents.move_to_end(key)
        while len(_agents) > MAX_AGENTS:
            _agents.popitem(last=False)

    # 方案B' 契约兑现：每轮请求前检查约束词是否变更，变更则热更新 system prompt。
    # 必须在 _agents_lock 之外调用（内部做 os.stat + 可能重建 messages[0]，避免持锁做 IO）。
    # 开销：一次 os.stat（纳秒级），仅在约束词签名变化时才重建 prompt。
    agent.refresh_if_changed()
    return agent


# ============ 接口 ============

@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.get("/api")
def api_info():
    return {"message": "Agent API 运行中", "status": "ok"}


@app.get("/models")
def models():
    """
    返回可用模型列表，供前端下拉框渲染。
    契约：{"models":[{"id","label","type","vision","custom"}], "default": str}
    ⚠️ 改用 list_models() 实时返回（含运行时注册的模型），而非启动快照。
    """
    return {"models": list_models(), "default": DEFAULT_MODEL}


@app.post("/models/register")
def register_model_api(req: RegisterModelRequest):
    """
    运行时注册一个 OpenAI 兼容模型。
    契约返回：{"ok":bool, "data":{模型项}|None, "error":str|None}
    """
    result = register_model({
        "id": req.id,
        "api_key": req.api_key,
        "base_url": req.base_url,
        "label": req.label or req.id,
        "vision": req.vision,
    })
    if not result.get("ok"):
        # 按契约统一结构返回，但 HTTP 层仍是 200，由前端读 ok 判断
        return result
    return result


@app.delete("/models/{model_id}")
def delete_model_api(model_id: str):
    """
    删除运行时注册的模型（内置模型不可删）。
    契约返回：{"ok":bool, "data":None, "error":str|None}
    """
    return delete_model(model_id)


@app.get("/models/constraints")
def get_constraints(model: str = ""):
    """
    读取指定模型的约束词。
    model 缺省为空 → 回退旧全局文件（兼容）。
    契约返回：{"ok":true, "data":{"model":str,"constraint":str,"is_default":bool,"default":str}, "error":None}
    """
    return {"ok": True, "data": get_constraint_meta(model), "error": None}


@app.post("/models/constraints")
def update_constraints(req: ConstraintRequest):
    """
    修改指定模型的约束词（空串=恢复默认）。
    生效方式：【方案B'】改后无需清缓存/重启，Agent 会在下次请求时自行比对签名并刷新 system prompt。
    契约返回：{"ok":bool, "data":{"constraint":str,"is_default":bool}|None, "error":str|None}
    """
    return set_constraint(req.model, req.constraint)


# ============================================================
# 【新增】上游余额查询：按模型注册时的 api_key 查上游账户剩余额度
# ============================================================

@app.get("/models/balance")
def models_balance(force: bool = False):
    """
    并发查询全部模型的上游余额（local 模型返回不支持标记）。
    契约返回：{"ok":true, "data":{"balances":[BalanceInfo...], "checked_at":int}, "error":None}
    BalanceInfo 字段见 balance_service.py 文件头（不含 api_key）。
    """
    return list_all_balances(force_refresh=force)


@app.get("/models/balance/{model_id}")
def model_balance_api(model_id: str, force: bool = False):
    """
    单模型余额查询（TTL 缓存 60s；force=true 强刷）。
    模型不存在 → {"ok":false, "data":None, "error":"模型 {id} 不存在"}
    """
    return get_model_balance(model_id, force_refresh=force)


# ============================================================
# 【新增】平台用户计费体系
# ============================================================

@app.post("/billing/keys")
def create_key_api(req: CreateKeyRequest, x_admin_token: Optional[str] = Header(None)):
    """
    【管理】签发平台 Key（sk-xxx）。完整 api_key 仅本次响应可见。
    契约返回：{"ok":bool, "data":{"api_key":str, "api_key_masked":str, "user_id":str,
               "balance":int, "total_used":int, "created_at":int, "note":str, "disabled":bool}|None, "error":str|None}
    """
    _admin_guard(x_admin_token)
    return billing.create_key(req.user_id, req.init_tokens, req.note)


@app.get("/billing/keys")
def list_keys_api(x_admin_token: Optional[str] = Header(None)):
    """【管理】列出全部 Key（含完整 api_key，勿在前端展示页使用）。"""
    _admin_guard(x_admin_token)
    return billing.list_keys()


@app.post("/billing/recharge")
def recharge_api(req: RechargeRequest, x_admin_token: Optional[str] = Header(None)):
    """【管理】充值（tokens>0）。data=充值后账户视图。"""
    _admin_guard(x_admin_token)
    return billing.recharge(req.api_key, req.tokens)


@app.post("/billing/keys/disable")
def disable_key_api(req: KeyOpRequest, x_admin_token: Optional[str] = Header(None)):
    """【管理】启用/禁用 Key。data=账户视图。"""
    _admin_guard(x_admin_token)
    return billing.disable_key(req.api_key, req.disabled)


@app.get("/billing/balance")
def billing_balance(authorization: Optional[str] = Header(None)):
    """
    【用户】通过平台 Key 自助查询剩余 tokens。
    请求头：Authorization: Bearer <平台Key>
    契约返回：{"ok":bool, "data":{"api_key_masked","user_id","balance","total_used",...}|None, "error":str|None}
    """
    raw = _parse_bearer(authorization)
    if not raw:
        return {"ok": False, "data": None, "error": "缺少 Authorization: Bearer <平台Key>"}
    return billing.get_balance(raw)


@app.get("/billing/logs")
def billing_logs(limit: int = 20, x_admin_token: Optional[str] = Header(None)):
    """【管理】最近扣费/充值流水（最新在前）。契约：{"ok":true,"data":{"logs":[{ts,api_key(掩码),user_id,model,tokens,balance_after,source}...]},"error":None}"""
    _admin_guard(x_admin_token)
    return billing.list_logs(limit)


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, authorization: Optional[str] = Header(None)):
    """
    非流式接口（异步）。
    【计费】带 Authorization: Bearer <平台Key> 时启用：Key 无效 401 / 余额<=0 拒绝 402 /
    完成后按 agent.last_usage 扣费；user_id 以 Key 绑定账户为准（防冒用）。
    不带头 → 匿名模式，行为与旧版一致。
    """
    account, raw_key = _billing_precheck(authorization)
    user_id = account["user_id"] if account else request.user_id
    async with semaphore:
        try:
            agent = get_agent(user_id, request.model)
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(executor, agent.chat, request.message, request.images)
            # 计费：非流式用量从 agent.last_usage 读取（agent_with_memory 契约新增属性）
            if account:
                usage = getattr(agent, "last_usage", None) or {}
                if usage.get("total"):
                    res = billing.deduct(raw_key, usage["total"], model=request.model, source="chat")
                    if not res.get("ok"):
                        print(f"💰 计费警告: {res.get('error')}")
                    else:
                        print(f"💰 计费(chat): -{usage['total']} tokens，剩余 {(res.get('data') or {}).get('balance')}")
            return ChatResponse(response=reply)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest, authorization: Optional[str] = Header(None)):
    """
    流式接口（SSE，真流式：边生成边推送），支持指定模型。
    【计费】带 Authorization: Bearer <平台Key> 时启用（规则同 /chat）。
    【SSE 契约新增】事件类型 "billing_warning"：{"type":"billing_warning","error":str}，
    仅在扣费失败（如中途余额不足）时出现，前端可安全忽略未知事件类型。
    【计费依赖】usage/paused/abandoned/error 事件新增 baseline_total 字段
    （断点前已计费基线，见 agent_with_memory.py 计费契约）。
    """
    account, raw_key = _billing_precheck(authorization)
    user_id = account["user_id"] if account else request.user_id
    agent = get_agent(user_id, request.model)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def producer():
        try:
            for event in agent.chat_stream(request.message, max_iterations=request.max_iterations, images=request.images, resume=request.resume, abandon=request.abandon):
                loop.call_soon_threadsafe(queue.put_nowait, event)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "error": str(e)})
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    async def generate():
        async with semaphore:
            executor.submit(producer)

            # ===== 计费钩子（仅当请求带有效平台 Key 时生效）=====
            # 契约：usage/paused/abandoned/error 事件的 tokens.total 为「全轮累计」，
            # baseline_total 为断点前已计费基线；扣增量 = total - 已扣累计。
            billed = {"v": None}

            def bill_event(event):
                if not account:
                    return None
                total = (event.get("tokens") or {}).get("total")
                if total is None:
                    return None
                base = event.get("baseline_total", 0)
                if billed["v"] is None:
                    billed["v"] = base
                delta = total - billed["v"]
                if delta <= 0:
                    return None
                res = billing.deduct(raw_key, delta, model=request.model, source="chat_stream")
                billed["v"] = total
                if not res.get("ok"):
                    print(f"💰 计费警告: {res.get('error')}")
                    return {"type": "billing_warning", "error": res.get("error", "扣费失败")}
                bal = (res.get("data") or {}).get("balance")
                print(f"💰 计费(stream): +{delta} tokens，剩余 {bal}")
                return None

            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    warn = bill_event(event)
                    if warn:
                        yield f"data: {json.dumps(warn, ensure_ascii=False)}\n\n"
                    if event.get("type") == "content":
                        yield f"data: {json.dumps({'content': event['content'], 'done': False}, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "usage":
                        yield f"data: {json.dumps({'type': 'usage', 'tokens': event['tokens'], 'done': False}, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "paused":
                        yield f"data: {json.dumps({'type': 'paused', 'tokens': event['tokens'], 'paused': True, 'need_rollback': event.get('need_rollback', False), 'done': False}, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "abandoned":
                        yield f"data: {json.dumps({'type': 'abandoned', 'tokens': event['tokens'], 'need_rollback': event.get('need_rollback', False), 'done': False}, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "error":
                        yield f"data: {json.dumps({'error': event.get('error', 'unknown error'), 'done': True}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'content': '', 'done': True}, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e), 'done': True}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@app.post("/reset")
async def reset(user_id: str = "default_user", model: str = DEFAULT_MODEL):
    """重置指定用户+模型的对话"""
    agent = get_agent(user_id, model)
    agent.reset()
    return {"message": "对话已重置"}


@app.get("/stats")
def stats():
    return get_stats()


@app.post("/report")
async def create_report(request: ReportRequest):
    try:
        from multi_agent_supervisor import generate_report
        loop = asyncio.get_running_loop()
        async with semaphore:
            report = await loop.run_in_executor(executor, generate_report, request.topic)
        return {"report": report}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/knowledge")
async def knowledge_ask(request: KnowledgeRequest):
    try:
        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(executor, ask_knowledge, request.question)
        return {"answer": answer}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
