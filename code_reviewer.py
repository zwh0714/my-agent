# code_reviewer.py
import os
from typing import TypedDict, Literal
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from tools import execute_tool

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")

model = ChatOpenAI(
    model="deepseek-chat",
    openai_api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
    temperature=0.2,
)


class ReviewState(TypedDict):
    file_path: str
    code: str
    issues: str
    suggestions: str
    fixed_code: str
    summary: str
    next_step: str


def reader_agent(state: ReviewState) -> ReviewState:
    print(f"📖 [读取] 正在读取: {state['file_path']}")
    file_path = state["file_path"]
    if not file_path.endswith((".py", ".txt", ".md", ".json", ".yaml", ".yml")):
        return {**state, "code": "", "summary": f"❌ 请输入有效的文件名（如 `tools.py`），而不是：{file_path}", "next_step": "end"}
    result = execute_tool("read_file", {"file_path": file_path})
    if result.startswith("❌"):
        return {**state, "code": "", "summary": result, "next_step": "end"}
    print(f"✅ [读取] 完成，共 {len(result)} 字符")
    return {**state, "code": result, "next_step": "analyze"}


def analyzer_agent(state: ReviewState) -> ReviewState:
    print(f"🔍 [分析] 正在分析代码...")
    messages = [
        SystemMessage(content="""你是一个资深 Python 代码审查专家。请仔细分析这段代码，找出以下问题：
1. 语法错误 2. 逻辑错误 3. 风格问题 4. 性能问题 5. 安全问题
请用列表形式输出。如果代码质量很好，请说"未发现明显问题"。
请务必用中文输出所有内容。"""),
        HumanMessage(content=f"请审查以下代码：\n\n{state['code']}")
    ]
    response = model.invoke(messages)
    print(f"✅ [分析] 完成")
    return {**state, "issues": response.content, "next_step": "suggest"}


def suggester_agent(state: ReviewState) -> ReviewState:
    print(f"💡 [建议] 正在生成改进建议...")
    messages = [
        SystemMessage(content="""你是一个 Python 重构专家。根据发现的问题，给出具体的改进建议。
要求：1. 针对每个问题给出修改方案 2. 代码修改给出对比 3. 按优先级排序 4. 用 Markdown 格式输出。
请务必用中文输出所有内容。"""),
        HumanMessage(content=f"代码问题：\n{state['issues']}\n\n原始代码：\n{state['code']}\n\n请给出具体的改进建议。")
    ]
    response = model.invoke(messages)
    print(f"✅ [建议] 完成")
    return {**state, "suggestions": response.content, "next_step": "summarize"}


def summarizer_agent(state: ReviewState) -> ReviewState:
    print(f"📝 [总结] 正在生成审查报告...")
    messages = [
        SystemMessage(content="""你是一个代码审查报告撰写者。请生成一份简洁的审查报告。
格式：
# 代码审查报告
## 文件
## 总体评价
## 关键问题
## 改进建议
## 审查结论
要求：简洁明了，重点突出。请务必用中文输出所有内容。"""),
        HumanMessage(content=f"文件名：{state['file_path']}\n\n发现问题：\n{state['issues']}\n\n改进建议：\n{state['suggestions']}")
    ]
    response = model.invoke(messages)
    print(f"✅ [总结] 完成")
    return {**state, "summary": response.content, "next_step": "end"}


def route(state: ReviewState) -> Literal["reader", "analyzer", "suggester", "summarizer", "end"]:
    step = state.get("next_step", "reader")
    if step == "analyze":
        return "analyzer"
    elif step == "suggest":
        return "suggester"
    elif step == "summarize":
        return "summarizer"
    elif step == "end":
        return "end"
    else:
        return "reader"


def build_review_workflow():
    workflow = StateGraph(ReviewState)
    workflow.add_node("reader", reader_agent)
    workflow.add_node("analyzer", analyzer_agent)
    workflow.add_node("suggester", suggester_agent)
    workflow.add_node("summarizer", summarizer_agent)
    workflow.set_entry_point("reader")
    workflow.add_conditional_edges("reader", route, {"analyzer": "analyzer", "end": END})
    workflow.add_conditional_edges("analyzer", route, {"suggester": "suggester", "end": END})
    workflow.add_conditional_edges("suggester", route, {"summarizer": "summarizer", "end": END})
    workflow.add_edge("summarizer", END)
    return workflow.compile()


def review_code(file_path: str) -> str:
    workflow = build_review_workflow()
    initial_state = {
        "file_path": file_path, "code": "", "issues": "",
        "suggestions": "", "fixed_code": "", "summary": "", "next_step": "reader"
    }
    print(f"\n{'='*50}\n🔍 开始审查：{file_path}\n{'='*50}")
    result = workflow.invoke(initial_state)
    print(f"\n{'='*50}\n✅ 审查完成\n{'='*50}\n")
    return result["summary"]


if __name__ == "__main__":
    file_path = input("请输入要审查的文件名：")
    report = review_code(file_path)
    print("\n" + "="*50 + "\n📋 审查报告：\n" + "="*50)
    print(report)
