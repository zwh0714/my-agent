# multi_agent_supervisor.py
import os
from typing import TypedDict, Literal
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")

model = ChatOpenAI(
    model="deepseek-chat",
    openai_api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
    temperature=0.3,
)


class TeamState(TypedDict):
    topic: str
    research: str
    draft: str
    review: str
    final_report: str
    next_agent: str
    iteration: int
    max_iteration: int
    history: list


def supervisor_agent(state: TeamState) -> TeamState:
    print(f"\n👔 [主管] 正在决策... (第 {state['iteration']} 轮)")
    status = f"""
当前任务状态：
- 主题：{state['topic']}
- 资料搜集：{'✅ 已完成' if state['research'] else '❌ 未完成'}
- 初稿撰写：{'✅ 已完成' if state['draft'] else '❌ 未完成'}
- 审核意见：{'✅ 已完成' if state['review'] else '❌ 未完成'}
- 当前迭代次数：{state['iteration']}/{state['max_iteration']}

请决定下一步由谁来做：researcher / writer / reviewer / finish
只输出一个词。
"""
    messages = [
        SystemMessage(content="你是一个项目主管，根据当前状态决定下一步由谁来做。只输出一个词。"),
        HumanMessage(content=status)
    ]
    response = model.invoke(messages)
    decision = response.content.strip().lower()
    for keyword in ["researcher", "writer", "reviewer", "finish"]:
        if keyword in decision:
            decision = keyword
            break
    else:
        decision = "finish"
    print(f"👔 [主管] 决定：{decision}")
    return {
        **state,
        "next_agent": decision,
        "iteration": state["iteration"] + (1 if decision == "writer" else 0),
        "history": state["history"] + [f"主管 → {decision}"]
    }


def researcher_agent(state: TeamState) -> TeamState:
    print(f"🔍 [研究员] 开始研究: {state['topic']}")
    messages = [
        SystemMessage(content="你是一个专业的研究员。针对给定主题，列出 5-8 个关键信息点。直接输出要点。"),
        HumanMessage(content=f"研究主题：{state['topic']}")
    ]
    response = model.invoke(messages)
    print(f"✅ [研究员] 完成")
    return {**state, "research": response.content, "history": state["history"] + ["研究员：资料搜集完成"]}


def writer_agent(state: TeamState) -> TeamState:
    print(f"✍️ [写手] 开始撰写 (第 {state['iteration']} 次)")
    if state["review"]:
        prompt = f"根据审核意见修改报告。\n\n主题：{state['topic']}\n当前报告：\n{state['draft']}\n\n审核意见：\n{state['review']}\n\n请输出修改后的完整报告。"
    else:
        prompt = f"根据资料写一份调研报告。\n\n主题：{state['topic']}\n资料：\n{state['research']}\n\n结构：引言、核心内容、应用场景、未来展望、总结。800字左右。"
    response = model.invoke([HumanMessage(content=prompt)])
    print(f"✅ [写手] 完成")
    return {**state, "draft": response.content, "review": "", "history": state["history"] + [f"写手：完成第 {state['iteration']} 版"]}


def reviewer_agent(state: TeamState) -> TeamState:
    print(f"🔎 [审核] 开始审核")
    messages = [
        SystemMessage(content="审核这份调研报告。输出：\n结论：PASS 或 FAIL\n意见：..."),
        HumanMessage(content=f"报告内容：\n{state['draft']}")
    ]
    response = model.invoke(messages)
    print(f"✅ [审核] 完成")
    return {**state, "review": response.content, "final_report": state["draft"], "history": state["history"] + [f"审核：{response.content[:30]}..."]}


def route_supervisor(state: TeamState) -> Literal["researcher", "writer", "reviewer", "finish"]:
    return state["next_agent"]


def build_workflow():
    workflow = StateGraph(TeamState)
    workflow.add_node("supervisor", supervisor_agent)
    workflow.add_node("researcher", researcher_agent)
    workflow.add_node("writer", writer_agent)
    workflow.add_node("reviewer", reviewer_agent)
    workflow.set_entry_point("supervisor")
    workflow.add_conditional_edges("supervisor", route_supervisor, {
        "researcher": "researcher",
        "writer": "writer",
        "reviewer": "reviewer",
        "finish": END
    })
    workflow.add_edge("researcher", "supervisor")
    workflow.add_edge("writer", "supervisor")
    workflow.add_edge("reviewer", "supervisor")
    return workflow.compile()


def generate_report(topic: str, max_iteration: int = 3) -> str:
    workflow = build_workflow()
    initial_state = {
        "topic": topic, "research": "", "draft": "", "review": "",
        "final_report": "", "next_agent": "", "iteration": 0,
        "max_iteration": max_iteration, "history": []
    }
    print(f"\n{'='*50}\n🚀 开始生成报告：{topic}\n{'='*50}")
    result = workflow.invoke(initial_state)
    print(f"\n{'='*50}\n✅ 报告生成完成\n📋 执行历史：")
    for h in result["history"]:
        print(f"   - {h}")
    print(f"{'='*50}\n")
    return result["final_report"]


if __name__ == "__main__":
    topic = input("请输入调研主题：")
    report = generate_report(topic, max_iteration=3)
    print("\n" + "="*50 + "\n📄 最终报告：\n" + "="*50)
    print(report)
