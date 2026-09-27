# agent_with_memory.py
__import__('pysqlite3')
import sys
sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')

import os
import json
import time
from openai import OpenAI
from tools import get_tools_schema, execute_tool
from memory import init_db, load_conversation, save_conversation
from history_vector import HistoryVectorStore
from model_router import get_client, get_contract, get_model_type, get_constraint, constraint_signature
from observability import (
    new_request_id, log_request_start, log_request_end,
    log_llm_call, log_agent_transition
)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")

init_db()

# 默认模型（未显式传 model 时使用）
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "deepseek-chat")

SYSTEM_PROMPT = """你是一个智能助手，可以根据用户需求调用合适的工具来解决问题。

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


def build_system_prompt(model: str) -> str:
    """
    构建 system prompt：基础提示 + 项目契约。
    契约由 model_router.get_contract() 统一提供，保证「本地/API 两个模型」看到同一份接口约定，
    这是「两个模型负责不同模块时接口能契合」的关键。
    """
    mtype = get_model_type(model)
    model_tag = f"【当前模型：{model}（{mtype}）】\n"
    # 约束词：按模型独立（方案A，一模型一文件）。
    # 【修复】拼接顺序改为「契约在前、约束词压轴」：
    #   模型对 system prompt 是首尾高权重、中间衰减。旧序把约束词夹在中间、契约占结尾，
    #   导致约束词（如“以XX结尾”）被契约文案淹没、不生效。
    #   现让 constraint 成为 system 的最后一句 = 紧贴回答的强指令，权重最高。
    #   契约内容一字不减，仍在 prompt 内（“不可被覆盖”是内容层语义，非位置层）。
    constraint = get_constraint(model)
    return model_tag + get_contract() + "\n\n" + constraint


def _build_user_content(text: str, images=None):
    """
    构造发给模型的 user content。

    契约（方案A）：
      images: Optional[List[str]]，元素为 base64 dataURL，如 "data:image/png;base64,..."。
      - 无 images：返回纯字符串 text（保持老行为，兼容所有模型）。
      - 有 images：返回 OpenAI 多模态数组：
          [{"type": "text", "text": text},
           {"type": "image_url", "image_url": {"url": <dataURL>}}, ...]
      注意：仅 vision 模型可接收该数组；调用方需自行保证。
    """
    if not images:
        return text
    content = [{"type": "text", "text": text or ""}]
    for img in images:
        if img:
            content.append({"type": "image_url", "image_url": {"url": img}})
    return content


def _sanitize_sequence(messages: list) -> list:
    """
    清洗消息序列，保证满足 OpenAI/DeepSeek 的硬约束：
      1. 带 tool_calls 的 assistant 后面必须紧跟「足量且 id 一一对应」的 tool 消息；
      2. 孤立的 tool 消息（其 tool_call_id 没被任何前序 assistant 声明）必须丢弃；
      3. tool 消息不能紧跟在 user/system/另一个 assistant 后面。

    这是运行期发送请求前的最后一道防线，避免把非法序列发给 API 导致 400。
    """
    cleaned = []
    # 当前已经声明、但还没收到结果的 tool_call id 集合（必须按顺序补齐）
    pending_ids = []

    for msg in messages:
        role = msg.get("role")

        if role == "assistant" and msg.get("tool_calls"):
            # 如果上一条 assistant 的 tool_calls 还没补齐结果，丢弃这条（序列已断，无法修复）
            if pending_ids:
                # 丢弃不完整的旧 assistant + 其未补齐的 tool（它们已无效）
                # 从尾部回滚：删掉上一个 assistant(tool_calls) 起的所有消息
                while cleaned and not (
                    cleaned[-1].get("role") == "assistant" and cleaned[-1].get("tool_calls")
                ):
                    cleaned.pop()
                if cleaned:
                    cleaned.pop()  # 删掉那个 assistant(tool_calls) 本身
                pending_ids = []
            cleaned.append(msg)
            pending_ids = [tc.get("id") for tc in msg["tool_calls"]]

        elif role == "tool":
            if pending_ids and msg.get("tool_call_id") == pending_ids[0]:
                cleaned.append(msg)
                pending_ids.pop(0)
            else:
                # 孤立 / 乱序的 tool：丢弃
                continue

        else:  # system / user / 普通 assistant
            if pending_ids:
                # assistant 的 tool_calls 没被补齐，却又来了新消息 → 整体作废这段工具链
                while cleaned and not (
                    cleaned[-1].get("role") == "assistant" and cleaned[-1].get("tool_calls")
                ):
                    cleaned.pop()
                if cleaned:
                    cleaned.pop()
                pending_ids = []
            cleaned.append(msg)

    # 结尾若还有未补齐的 tool_calls，删掉这段悬空的 assistant
    if pending_ids:
        while cleaned and not (
            cleaned[-1].get("role") == "assistant" and cleaned[-1].get("tool_calls")
        ):
            cleaned.pop()
        if cleaned:
            cleaned.pop()

    return cleaned


def _safe_recent(messages: list, n: int = 30) -> list:
    """
    从 messages 尾部取最近 n 条，但**不会从工具链中间切开**。
    如果切点落在一段 assistant(tool_calls) + tool... 中间，则把整段一并纳入。

    注意：传入的 messages 不应包含开头的 system（调用方单独处理 system）。
    """
    if len(messages) <= n:
        return list(messages)

    start = len(messages) - n
    # 向前回溯：如果切点落在 tool 链内部（tool 结果 / 带 tool_calls 的 assistant），
    # 一直回溯到该链最前面那个 assistant(tool_calls)，保证整段成对纳入，不被从中间切开。
    while start > 0:
        cur = messages[start]
        if cur.get("role") == "tool":
            start -= 1
            continue
        if cur.get("role") == "assistant" and cur.get("tool_calls"):
            # 切点正好是链首 assistant(tool_calls)：从这里开始即合法，停止回溯
            break
        # 遇到普通消息（user / 无 tool_calls 的 assistant）：说明切点已是合法边界
        break
    return messages[start:]


class AgentWithMemory:
    def __init__(self, user_id: str = "default_user", model: str = DEFAULT_MODEL):
        """
        :param user_id: 记忆命名空间 key。多模型场景下应传入 "用户::模型" 形式的隔离 key，
                        由上层（api.py）负责拼接，保证不同模型记忆互不污染。
        :param model:   模型名（如 "deepseek-chat" / "qwen2.5:7b"），由 model_router 决定用哪个 client。
        """
        self.user_id = user_id
        self.model = model
        self.model_type = get_model_type(model)
        # 按模型名拿对应 client（本地→Ollama，API→DeepSeek）
        self.client = get_client(model)

        self.messages = load_conversation(user_id)
        self.tools = get_tools_schema()

        # ===== 断点续跑状态（未完成任务的暂存）=====
        # 契约：paused_state = {
        #   "context_messages": list,   # 发送给模型的完整合法序列（可继续追加）
        #   "total_tokens": {"prompt":int,"completion":int,"total":int},
        #   "iteration": int,           # 已完成轮数
        #   "partial_tool_chain": bool, # 断点是否停在工具链中间（决定是否建议回滚）
        #   "user_input": str,          # 原始用户输入（用于回滚/提示）
        # } | None
        self.paused_state = None
        self.vector_store = HistoryVectorStore()

        # 【方案B'·修复】原子化：先采签名，再据此构建 system prompt。
        # 修复前顺序是「先建 prompt、后采签名」，两者之间存在采样窗口：
        #   若文件恰在窗口内变化，prompt 会是旧内容，而签名是新的，
        #   导致 refresh_if_changed() 误判「无变化」，从而长期沿用过期 prompt。
        # 现改为：先采签名 → 立即用当前约束词构建 prompt → 用同一逻辑时刻的签名落库，
        # 保证 self.messages[0] 与 self._constraint_sig 始终一致。
        self._constraint_sig = constraint_signature(self.model)
        _initial_prompt = build_system_prompt(self.model)

        # 注入 system prompt（含项目契约）
        if not self.messages or self.messages[0].get("role") != "system":
            self.messages.insert(0, {"role": "system", "content": _initial_prompt})
        else:
            # 已有 system：刷新为最新提示（含契约），保证契约变更后能生效
            self.messages[0] = {"role": "system", "content": _initial_prompt}

        self._sync_history_to_vector()
        self._clean_history()
        print(f"🔧 [{self.model}] 已加载工具: {[t['function']['name'] for t in self.tools]}")
        print(f"📂 [{self.model}] 已加载 {len(self.messages)} 条历史记忆")

    def _sync_history_to_vector(self):
        try:
            count = self.vector_store.collection.count()
            if count == 0:
                print(f"📦 [{self.model}] 首次启动，正在同步历史到向量库...")
                for i, msg in enumerate(self.messages):
                    if msg.get("role") in ("user", "assistant"):
                        self.vector_store.add_message(self.user_id, str(i), msg["role"], msg["content"])
        except Exception as e:
            print(f"⚠️ 同步历史失败: {e}")

    def _clean_history(self):
        """清理损坏的历史（统一走 _sanitize_sequence，保证 tool 链成对）"""
        system_msgs = [m for m in self.messages if m.get("role") == "system"]
        non_system = [m for m in self.messages if m.get("role") != "system"]
        self.messages = system_msgs[:1] + _sanitize_sequence(non_system)

    def _get_relevant_context(self, user_input: str, top_k: int = 8) -> list:
        """
        构建上下文：system + 相关历史片段(system) + 最近对话
        最近对话使用 _safe_recent 截取，且整体再过一遍 _sanitize_sequence，杜绝非法序列。
        """
        combined = []
        for msg in self.messages:
            if msg.get("role") == "system":
                combined.append(msg)
                break

        relevant = self.vector_store.search_relevant(self.user_id, user_input, top_k=top_k)
        if relevant:
            history_text = "【历史对话片段（仅供参考，可能和当前问题无关）】\n"
            for msg in relevant:
                if msg["role"] in ("user", "assistant"):
                    history_text += f"{msg['role']}: {msg['content'][:200]}\n"
            combined.append({"role": "system", "content": history_text})

        non_system = [m for m in self.messages if m.get("role") != "system"]
        recent = _safe_recent(non_system, 30)
        combined.extend(recent)

        return _sanitize_sequence(combined)

    @staticmethod
    def _apply_user_content(context_messages: list, user_input: str, send_user: dict) -> list:
        """
        把 context 中「最后一条 user 消息」替换为 send_user（可能是多模态 content）。
        仅当 send_user 的 content 不是 str（即真的带图）时才替换，
        避免对纯文本请求产生任何行为差异。
        找不到对应 user 消息时原样返回。
        """
        if isinstance(send_user.get("content"), str):
            return context_messages
        for i in range(len(context_messages) - 1, -1, -1):
            m = context_messages[i]
            if m.get("role") == "user" and m.get("content") == user_input:
                context_messages = list(context_messages)
                context_messages[i] = send_user
                break
        return context_messages

    def _build_tool_followup(self, user_input: str, tool_round_msgs: list) -> list:
        """工具调用后：在相关上下文基础上，追加本轮所有 assistant(tool_calls) + 对应 tool 结果"""
        context_messages = self._get_relevant_context(user_input)
        context_messages.extend(tool_round_msgs)
        return _sanitize_sequence(context_messages)

    def _run_tool_calls(self, message, request_id: str) -> list:
        """执行 assistant 消息中的所有 tool_calls，返回本轮新增的消息列表"""
        round_msgs = [message.model_dump()]
        self.messages.append(message.model_dump())

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            try:
                result = execute_tool(tool_name, arguments, request_id=request_id)
            except Exception as e:
                result = f"工具执行失败: {str(e)}"
            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result
            }
            round_msgs.append(tool_msg)
            self.messages.append(tool_msg)

        return round_msgs

    def refresh_if_changed(self) -> bool:
        """
        【方案B'·契约新增】检查约束词是否变更，变了则重建 system prompt。

        机制：
          比对自己记录的上次签名 self._constraint_sig 与磁盘当前签名
          （model_router.constraint_signature(model)，仅 stat 元数据、不读文件内容）。
          不同 → 用最新约束词重建 self.messages[0] 的 system prompt，并更新签名。

        返回：True=本次发生了刷新；False=无变化。

        成本：每轮请求一次 os.stat（纳秒~微秒级）；仅在约束词真的变化时才重建 prompt。
        线程安全：仅改自身 messages[0]，与其它 agent 实例无关。
        """
        try:
            sig = constraint_signature(self.model)
        except Exception:
            return False
        if sig == getattr(self, "_constraint_sig", None):
            return False
        # 约束词变了：重建 system prompt（保留历史，只替换第 0 条）
        prompt = build_system_prompt(self.model)
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0] = {"role": "system", "content": prompt}
        else:
            self.messages.insert(0, {"role": "system", "content": prompt})
        self._constraint_sig = sig
        print(f"♻️ [{self.model}] 检测到约束词变更，已刷新 system prompt")
        return True

    def chat(self, user_input: str, images=None) -> str:
        self.refresh_if_changed()   # 方案B'：约束词变更兜底刷新
        request_id = new_request_id()
        start_time = time.time()
        log_request_start(user_input, request_id)

        # 历史/落库存纯文本；发给模型的那条按需带图（多模态）
        self.messages.append({"role": "user", "content": user_input})
        _send_user = {"role": "user", "content": _build_user_content(user_input, images)}
        self.vector_store.add_message(self.user_id, str(len(self.messages) - 1), "user", user_input)

        try:
            context_messages = self._get_relevant_context(user_input)
            context_messages = self._apply_user_content(context_messages, user_input, _send_user)
            response = self.client.chat.completions.create(
                model=self.model,          # ← 按实例模型
                messages=_sanitize_sequence(context_messages),
                tools=self.tools,
                tool_choice="auto",
            )
            log_llm_call(self.model, response.usage, request_id)
            message = response.choices[0].message

            if message.tool_calls:
                round_msgs = self._run_tool_calls(message, request_id)
                context_messages = self._build_tool_followup(user_input, round_msgs)
                final_response = self.client.chat.completions.create(
                    model=self.model,      # ← 按实例模型
                    messages=_sanitize_sequence(context_messages),
                )
                log_llm_call(self.model, final_response.usage, request_id)
                final_message = final_response.choices[0].message
                self.messages.append(final_message.model_dump())
                reply = final_message.content
            else:
                self.messages.append(message.model_dump())
                reply = message.content

            self.vector_store.add_message(self.user_id, str(len(self.messages) - 1), "assistant", reply)
            save_conversation(self.messages, self.user_id)
            log_request_end(request_id, (time.time() - start_time) * 1000, success=True)
            return reply
        except Exception as e:
            log_request_end(request_id, (time.time() - start_time) * 1000, success=False)
            raise

    def chat_stream(self, user_input: str, max_iterations: int = 25, images=None,
                    resume: bool = False, abandon: bool = False):
        """
        流式版本（支持自主循环 + 断点续跑 + 中途放弃）。

        退出路径与 token 语义（契约）：
          - 正常答完 / 达到 max_iterations / 异常 / 用户放弃，**每条路径都 yield 一次
            全轮累计 usage**（不是单轮）。前端收到 usage 即累加，不会重复。
          - 达到 max_iterations 时进入「暂停态」：额外 yield 一个
            {"type":"paused", "tokens":{累计}, "paused":true, "need_rollback":bool}，
            并把断点存入 self.paused_state，等待前端「继续 / 不继续」。
          - resume=True：从 self.paused_state 恢复 context_messages 继续跑。
            【契约变更 2024-xx】resume 时 max_iterations 语义 = 本次续跑允许的新增轮数
            （iteration 归零重计，默认 25 即续跑最多再 25 轮）；不再沿用断点前的已用轮数。
          - abandon=True：用户选择不继续 → 输出最终累计 tokens + 回滚建议，清理暂停态。

        契约变更登记：
          SSE 事件 "usage" 的 tokens 语义 = 本次请求「全轮累计」用量（旧版是单轮）。
          SSE 新增 "paused" 事件：{type:"paused", tokens:{...}, paused:true, need_rollback:bool}
          SSE 新增 "abandoned" 事件：{type:"abandoned", tokens:{...}, need_rollback:bool, message:str}
          【P2-2 契约变更】暂停提示不再经 "content" 事件下发（旧版前端需正则剥离，属「文本即接口」）：
            暂停信息唯一载体 = "paused" 事件，字段不变；前端已同步删除 stripPauseTip。
        """
        self.refresh_if_changed()   # 方案B'：约束词变更兜底刷新
        request_id = new_request_id()
        log_request_start(user_input, request_id)

        # ===== 放弃分支：用户选择不继续 =====
        if abandon:
            ps = self.paused_state
            total = (ps or {}).get("total_tokens", {"prompt": 0, "completion": 0, "total": 0})
            need_rollback = bool((ps or {}).get("partial_tool_chain"))
            msg = ("\n\n✋ 已按你的选择放弃继续执行。\n"
                   f"本次累计消耗：{total['total']} tokens（入 {total['prompt']} / 出 {total['completion']}）。\n"
                   + ("⚠️ 任务停在工具调用链中间，半截的工具结果可能影响后续对话，建议回滚到任务开始前。"
                      if need_rollback else
                      "✅ 未产生半截工具结果，无需回滚，可安全继续后续对话。"))
            self.paused_state = None
            yield {"type": "content", "content": msg}
            yield {"type": "abandoned", "tokens": total,
                   "need_rollback": need_rollback, "message": msg}
            log_request_end(request_id, 0, success=True)
            return

        # ===== 恢复分支 / 全新分支 =====
        if resume and self.paused_state:
            ps = self.paused_state
            context_messages = ps["context_messages"]
            total_prompt = ps["total_tokens"]["prompt"]
            total_completion = ps["total_tokens"]["completion"]
            # 【P0 修复】resume 时迭代预算重置为 0：
            # 旧逻辑 iteration = ps["iteration"]（已等于上限，如 25），while iteration < max_iterations
            # 恒为 False → 循环一次都不跑 → 立即再次进入暂停分支，"继续执行"按钮形同虚设。
            # 契约变更：resume=True 时 max_iterations 语义 = 本次续跑允许的新增轮数（默认 25 即再给 25 轮）；
            # total_tokens 仍从断点前累计值继续累加，token 统计跨暂停不断档。
            iteration = 0
            self.paused_state = None
            # 恢复时不再 append user / 不再重建上下文
        else:
            # 历史/落库存纯文本；发给模型的那条按需带图（多模态）
            self.messages.append({"role": "user", "content": user_input})
            self.vector_store.add_message(self.user_id, str(len(self.messages) - 1), "user", user_input)
            _send_user = {"role": "user", "content": _build_user_content(user_input, images)}

            # 只在循环外构建一次上下文；之后持续在同一条合法序列上追加
            context_messages = self._get_relevant_context(user_input)
            context_messages = self._apply_user_content(context_messages, user_input, _send_user)
            total_prompt = 0
            total_completion = 0
            iteration = 0

        def _acc_usage(u):
            nonlocal total_prompt, total_completion
            if u:
                total_prompt += u.prompt_tokens
                total_completion += u.completion_tokens

        def _emit_usage():
            return {"type": "usage", "tokens": {
                "prompt": total_prompt,
                "completion": total_completion,
                "total": total_prompt + total_completion,
            }}

        try:
            while iteration < max_iterations:
                iteration += 1
                print(f"🔄 [{self.model}] 第 {iteration} 轮")

                # 发送前最后清洗，杜绝任何非法序列
                response = self.client.chat.completions.create(
                    model=self.model,      # ← 按实例模型
                    messages=_sanitize_sequence(context_messages),
                    tools=self.tools,
                    tool_choice="auto",
                )
                log_llm_call(self.model, response.usage, request_id)
                _acc_usage(response.usage)
                message = response.choices[0].message

                # 无工具调用 → 最终答复，逐字流式输出
                if not message.tool_calls:
                    msg_dict = message.model_dump()
                    self.messages.append(msg_dict)
                    context_messages.append(msg_dict)
                    reply = message.content or ""
                    for char in reply:
                        yield {"type": "content", "content": char}
                    yield _emit_usage()
                    self.vector_store.add_message(self.user_id, str(len(self.messages) - 1), "assistant", reply)
                    save_conversation(self.messages, self.user_id)
                    self.paused_state = None
                    log_request_end(request_id, 0, success=True)
                    return

                # 有工具调用：assistant(tool_calls) 入列，然后依次补 tool 结果
                assistant_msg = message.model_dump()
                self.messages.append(assistant_msg)
                context_messages.append(assistant_msg)

                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    try:
                        arguments = json.loads(tool_call.function.arguments or "{}")
                    except Exception:
                        arguments = {}
                    print(f"🔧 [{self.model}] 调用工具: {tool_name}")

                    try:
                        result = execute_tool(tool_name, arguments, request_id=request_id)
                    except Exception as e:
                        result = f"工具执行失败: {str(e)}"

                    tool_msg = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result
                    }
                    self.messages.append(tool_msg)
                    context_messages.append(tool_msg)

            # ===== 达到最大迭代：进入暂停态，等待用户决定 =====
            # 判断断点是否停在「工具链中间」：末条是 tool → 说明刚执行完工具，是合法边界；
            # 末条是 assistant(tool_calls) 且其 tool 结果不全 → 半截链，建议回滚。
            last = context_messages[-1] if context_messages else None
            partial_tool_chain = bool(
                last and last.get("role") == "assistant" and last.get("tool_calls")
            )

            total = {"prompt": total_prompt, "completion": total_completion,
                     "total": total_prompt + total_completion}
            self.paused_state = {
                "context_messages": context_messages,
                "total_tokens": total,
                "iteration": iteration,
                "partial_tool_chain": partial_tool_chain,
                "user_input": user_input,
            }
            save_conversation(self.messages, self.user_id)

            # 【P2-2】暂停提示不再塞进 content：旧版靠前端 stripPauseTip 正则剥离，属「文本即接口」，
            # 后端改一个字前端就剥不掉。暂停信息统一由下方 {"type": "paused"} 结构化事件承载，前端面板展示。
            yield _emit_usage()
            yield {"type": "paused", "tokens": total,
                   "paused": True, "need_rollback": partial_tool_chain}
            log_request_end(request_id, 0, success=True)

        except Exception as e:
            # 异常路径：也要把「已累计」的 token 报出去，不能白丢
            log_request_end(request_id, 0, success=False)
            total = {"prompt": total_prompt, "completion": total_completion,
                     "total": total_prompt + total_completion}
            yield _emit_usage()
            yield {"type": "error", "error": str(e), "tokens": total}


    def reset(self):
        """重置对话（保留 system 消息，含最新契约）"""
        self.messages = [{"role": "system", "content": build_system_prompt(self.model)}]
        self.paused_state = None   # 【P1 配套】重置时同步丢弃断点，避免残留可被 resume 的过期暂停态
        save_conversation(self.messages, self.user_id)
        print(f"🔄 [{self.model}] 对话已重置（长期记忆保留）")


if __name__ == "__main__":
    # 命令行测试：可指定模型 python agent_with_memory.py deepseek-chat
    model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    agent = AgentWithMemory(user_id=f"zwh::{model}", model=model)
    print("=" * 50)
    print(f"🧠 带长期记忆的 Agent 已启动！模型: {model}")
    print("输入 'exit' 退出，输入 'reset' 重置当前对话")
    print("=" * 50)
    while True:
        try:
            user_input = input("\n👤 你: ")
        except UnicodeDecodeError:
            print("⚠️ 输入编码错误，请重新输入")
            continue
        if user_input.lower() == 'exit':
            break
        if user_input.lower() == 'reset':
            agent.reset()
            continue
        try:
            response = agent.chat(user_input)
            print(f"🤖 AI: {response}")
        except Exception as e:
            print(f"❌ 错误: {e}")


        # 【调试】打印实际的 system 提示词
        print("=" * 80)
        print(f"🔧 [{self.model}] 实际的 System Prompt:")
        print("=" * 80)
        print(_initial_prompt)
        print("=" * 80)
