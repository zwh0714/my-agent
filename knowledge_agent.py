# knowledge_agent.py
import os
import json
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

class KnowledgeState(TypedDict):
    question: str           # 用户问题
    search_results: str     # 搜索结果
    answer: str             # 最终回答
    next_step: str          # 下一步

def searcher_agent(state: KnowledgeState) -> KnowledgeState:
    """搜索知识库：先用 grep 找相关文件，再读取内容"""
    print(f"🔍 [搜索] 正在搜索: {state['question']}")
    
    # 1. 用 run_command 执行 grep，找相关文件
    keywords = state['question'].replace('？', '').replace('?', '').split()
    keyword = keywords[0] if keywords else state['question'][:10]
    
    grep_cmd = f"grep -rl '{keyword}' knowledge/ 2>/dev/null | head -5"
    result = execute_tool("run_command", {"command": grep_cmd})
    
    print(f"✅ [搜索] grep 结果: {result[:100]}")
    return {**state, "search_results": result, "next_step": "read"}

def reader_agent(state: KnowledgeState) -> KnowledgeState:
    """读取搜索到的文件内容"""
    print(f"📖 [读取] 正在读取相关文件")
    
    # 从 grep 结果里提取文件名
    lines = state['search_results'].split('\n')
    files = [line.strip() for line in lines if line.strip().endswith(('.py', '.md', '.txt'))]
    
    content = ""
    for f in files[:3]:  # 最多读 3 个文件
        result = execute_tool("read_file", {"file_path": f})
        content += f"\n--- {f} ---\n{result[:2000]}\n"
    
    print(f"✅ [读取] 读取了 {len(files)} 个文件")
    return {**state, "search_results": content, "next_step": "answer"}

def answer_agent(state: KnowledgeState) -> KnowledgeState:
    """根据搜索结果生成回答"""
    print(f"💡 [回答] 正在生成回答")
    
    messages = [
        SystemMessage(content="""你是一个知识库助手。根据搜索到的内容，回答用户的问题。
如果搜索结果里没有相关信息，请如实告知用户。
回答要简洁、准确，引用具体文件内容。"""),
        HumanMessage(content=f"问题：{state['question']}\n\n搜索到的内容：\n{state['search_results']}")
    ]
    
    response = model.invoke(messages)
    print(f"✅ [回答] 完成")
    return {**state, "answer": response.content, "next_step": "end"}

def route(state: KnowledgeState) -> Literal["searcher", "reader", "answer", "end"]:
    step = state.get("next_step", "searcher")
    if step == "read":
        return "reader"
    elif step == "answer":
        return "answer"
    elif step == "end":
        return "end"
    return "searcher"

def build_knowledge_workflow():
    workflow = StateGraph(KnowledgeState)
    workflow.add_node("searcher", searcher_agent)
    workflow.add_node("reader", reader_agent)
    workflow.add_node("answer", answer_agent)
    workflow.set_entry_point("searcher")
    workflow.add_conditional_edges("searcher", route, {"reader": "reader", "end": END})
    workflow.add_conditional_edges("reader", route, {"answer": "answer", "end": END})
    workflow.add_edge("answer", END)
    return workflow.compile()

def ask_knowledge(question: str) -> str:
    """知识库问答的对外接口"""
    workflow = build_knowledge_workflow()
    initial_state = {
        "question": question,
        "search_results": "",
        "answer": "",
        "next_step": "searcher"
    }
    print(f"\n{'='*50}\n🔍 开始查询：{question}\n{'='*50}")
    result = workflow.invoke(initial_state)
    return result["answer"]

if __name__ == "__main__":
    question = input("请输入你的问题：")
    answer = ask_knowledge(question)
    print("\n" + "="*50 + "\n📋 回答：\n" + "="*50)
    print(answer)
