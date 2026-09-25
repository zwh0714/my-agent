# api.py
import os
import json
import threading
import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from agent_with_memory import AgentWithMemory, DEFAULT_MODEL
from typing import List
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
import uvicorn

# ============ 数据目录 ============
DATA_DIR = os.environ.get("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)

# ============ 服务端口（可被环境变量 PORT 覆盖，默认 8000） ============
PORT = int(os.environ.get("PORT", "8000"))

# ============ 并发配置 ============
MAX_CONCURRENCY = 8

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


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """非流式接口（异步）"""
    async with semaphore:
        try:
            agent = get_agent(request.user_id, request.model)
            loop = asyncio.get_running_loop()
            reply = await loop.run_in_executor(executor, agent.chat, request.message, request.images)
            return ChatResponse(response=reply)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """流式接口（SSE，真流式：边生成边推送），支持指定模型"""
    agent = get_agent(request.user_id, request.model)
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
            try:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
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
