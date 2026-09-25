# tools.py
import os
import time
import shlex
import subprocess
import requests
from datetime import datetime
from observability import track_tool_call

# ============ 工具函数定义 ============

# ============ 事务 / 写前快照（vibe coding 回滚） ============
import json as _json
import time as _time
import shutil as _shutil

TXN_ROOT = os.path.join(os.getcwd(), ".txn")
# 快照范围：项目内、非事务目录、非虚拟环境
TXN_IGNORE_PREFIXES = (".txn", ".venv", "__pycache__", ".git", "node_modules")


def _txn_dir(task_id: str) -> str:
    return os.path.join(TXN_ROOT, str(task_id))


def _rel_to_cwd(abs_path: str) -> str:
    return os.path.relpath(abs_path, os.getcwd())


def _is_txn_ignored(abs_path: str) -> bool:
    rel = _rel_to_cwd(abs_path)
    return any(rel.split(os.sep)[0] == p for p in TXN_IGNORE_PREFIXES)


def txn_begin(task_id: str) -> None:
    """创建事务目录（若不存在）"""
    d = _txn_dir(task_id)
    os.makedirs(os.path.join(d, "snapshots"), exist_ok=True)
    mf = os.path.join(d, "manifest.json")
    if not os.path.exists(mf):
        with open(mf, "w", encoding="utf-8") as f:
            _json.dump({"task_id": str(task_id), "created_at": _time.time(),
                        "files": [], "settled": False}, f, ensure_ascii=False, indent=2)


def _load_manifest(task_id: str) -> dict:
    mf = os.path.join(_txn_dir(task_id), "manifest.json")
    if not os.path.exists(mf):
        return None
    with open(mf, encoding="utf-8") as f:
        return _json.load(f)


def _save_manifest(task_id: str, data: dict) -> None:
    mf = os.path.join(_txn_dir(task_id), "manifest.json")
    with open(mf, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)


def txn_snapshot(task_id: str, file_path: str, action: str) -> None:
    """写前快照：该文件本次事务尚未快照过，则把【当前原内容】存一份。
    文件不存在也登记（回滚时删除该新文件）。"""
    if not task_id:
        return
    abs_path = os.path.abspath(file_path)
    if _is_txn_ignored(abs_path):
        return
    txn_begin(task_id)
    mf = _load_manifest(task_id)
    if mf is None:
        return
    rel = _rel_to_cwd(abs_path)
    if any(x["path"] == rel for x in mf["files"]):
        return  # 已快照过，保留最早的原始版本
    snap = os.path.join(_txn_dir(task_id), "snapshots", rel)
    existed = os.path.exists(abs_path)
    if existed:
        os.makedirs(os.path.dirname(snap), exist_ok=True)
        _shutil.copy2(abs_path, snap)
    mf["files"].append({"path": rel, "action": action,
                        "existed": existed, "snapshot": rel})
    _save_manifest(task_id, mf)


def txn_list(task_id: str) -> list:
    mf = _load_manifest(task_id)
    return mf["files"] if mf else []


def txn_rollback(task_id: str) -> dict:
    """把该事务快照过的文件还原到任务开始前。"""
    mf = _load_manifest(task_id)
    if mf is None:
        return {"ok": False, "data": None, "error": "事务不存在或已清理"}
    restored, errors = [], []
    for item in mf["files"]:
        rel = item["path"]
        cur = os.path.join(os.getcwd(), rel)
        snap = os.path.join(_txn_dir(task_id), "snapshots", rel)
        try:
            if item.get("existed"):
                if os.path.exists(snap):
                    os.makedirs(os.path.dirname(cur), exist_ok=True)
                    _shutil.copy2(snap, cur)
                    restored.append(rel)
                else:
                    errors.append(f"{rel}: 快照丢失")
            else:
                # 原文件不存在 → 回滚即删除新建的文件
                if os.path.exists(cur):
                    os.remove(cur)
                restored.append(f"{rel}（删除新建文件）")
        except Exception as e:
            errors.append(f"{rel}: {e}")
    if errors:
        return {"ok": False, "data": {"restored": restored}, "error": "; ".join(errors)}
    return {"ok": True, "data": {"restored": restored}, "error": None}


def txn_commit(task_id: str) -> None:
    """用户确认保留改动 → 删除事务目录"""
    d = _txn_dir(task_id)
    if os.path.exists(d):
        _shutil.rmtree(d, ignore_errors=True)


def get_weather(city: str) -> str:
    """查询指定城市的当前天气"""
    url = f"https://wttr.in/{city}?format=%C+%t&lang=zh"
    try:
        response = requests.get(url, timeout=5)
        return response.text.strip()
    except Exception as e:
        return f"抱歉，暂时无法获取 {city} 的天气信息: {str(e)}"


def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    """获取当前日期和时间"""
    now = datetime.now()
    return now.strftime("%Y年%m月%d日 %H:%M:%S")


def calculate(expression: str) -> str:
    """执行简单的数学计算"""
    try:
        allowed = set("0123456789+-*/(). ")
        if not all(c in allowed for c in expression):
            return "错误：表达式包含非法字符"
        result = eval(expression)
        return f"{expression} = {result}"
    except Exception as e:
        return f"计算错误：{str(e)}"


def web_search(query: str) -> str:
    """联网搜索最新信息"""
    try:
        api_key = os.environ.get("TAVILY_API_KEY")
        if not api_key:
            return "⚠️ 未配置 TAVILY_API_KEY，无法搜索"
        
        response = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 3,
            },
            timeout=10,
        )
        data = response.json()
        results = data.get("results", [])
        if results:
            summary = f"搜索结果（{query}）：\n"
            for i, r in enumerate(results[:3], 1):
                summary += f"{i}. {r.get('title', '')}\n"
                summary += f"   {r.get('content', '')[:200]}...\n"
                summary += f"   来源：{r.get('url', '')}\n\n"
            return summary
        return f"未找到关于 '{query}' 的搜索结果"
    except Exception as e:
        return f"搜索失败：{str(e)}"


def find_file_in_project(filename: str) -> str:
    """在当前目录及子目录中搜索文件"""
    cwd = os.getcwd()
    for root, dirs, files in os.walk(cwd):
        for file in files:
            if file == filename:
                return os.path.join(root, file)
            if filename in file and file.endswith('.py'):
                return os.path.join(root, file)
    return None


def read_file(file_path: str) -> str:
    """读取本地文件内容"""
    try:
        cwd = os.getcwd()
        abs_path = os.path.abspath(file_path)
        
        if os.path.exists(abs_path) and os.path.isfile(abs_path):
            if not abs_path.startswith(cwd):
                return f"❌ 安全限制：只能读取当前目录下的文件（{cwd}）"
        else:
            found_path = find_file_in_project(file_path)
            if found_path is None:
                return f"❌ 找不到文件：{file_path}"
            abs_path = found_path
        
        with open(abs_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        max_len = 20000
        if len(content) > max_len:
            content = content[:max_len] + f"\n\n... (文件太长，只显示前 {max_len} 字符)"
        
        return f"📄 文件内容（{abs_path}）：\n{content}"
    except UnicodeDecodeError:
        return f"❌ 无法读取文件：{file_path}（可能是二进制文件）"
    except Exception as e:
        return f"❌ 读取失败：{str(e)}"


def write_file(file_path: str, content: str, _task_id: str = None) -> str:
    """写入或覆盖文件内容"""
    try:
        cwd = os.getcwd()
        abs_path = os.path.abspath(file_path)
        
        if not abs_path.startswith(cwd):
            return f"❌ 安全限制：只能操作当前目录下的文件"
        
        # —— 事务快照（写前）——
        if _task_id:
            txn_snapshot(_task_id, abs_path, "write")
        
        # 自动备份原文件
        if os.path.exists(abs_path):
            import shutil
            shutil.copy2(abs_path, abs_path + ".bak")
        
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return f"✅ 文件已保存：{abs_path}\n共写入 {len(content)} 个字符"
    except Exception as e:
        return f"❌ 写入失败：{str(e)}"


def append_to_file(file_path: str, content: str, _task_id: str = None) -> str:
    """追加内容到文件末尾"""
    try:
        cwd = os.getcwd()
        abs_path = os.path.abspath(file_path)
        
        if not abs_path.startswith(cwd):
            return f"❌ 安全限制：只能操作当前目录下的文件"
        
        # —— 事务快照（写前）——
        if _task_id:
            txn_snapshot(_task_id, abs_path, "append")
        
        with open(abs_path, 'a', encoding='utf-8') as f:
            f.write(content)
        
        return f"✅ 已追加内容到：{abs_path}"
    except Exception as e:
        return f"❌ 追加失败：{str(e)}"


# ============ 终端命令执行（白名单 + 黑名单） ============

ALLOWED_COMMANDS = {
        # 查看/导航
        "ls", "pwd", "cd", "tree", "file", "stat", "du", "df",
        # 读取内容
        "cat", "head", "tail", "less", "more", "wc", "grep", "find", "echo",
        # 系统信息
        "whoami", "date", "uname", "env", "hostname", "uptime", "which", "whereis",
        # 版本控制
        "git", "python", "python3", "pip", "pip3", "node", "npm", "go", "java", "javac",
        # ============ 运维命令（新增） ============
        # 进程/内存
        "ps", "top", "htop", "free",
        # 网络
        "ss", "netstat", "ip", "ping", "lsof",
        # 日志
        "journalctl",
        # Docker（只允许查看类）
        "docker ps", "docker logs", "docker images", "docker inspect",
        # 服务（只允许查看状态）
        "systemctl status", "systemctl list-units",
        # 磁盘 I/O
        "iostat",
        }

FORBIDDEN_COMMANDS = {
    # 删除类
    "rm", "rmdir", "unlink", "shred",
    # 移动/复制类（危险）
    "mv", "dd",
    # 权限类
    "chmod", "chown", "chgrp",
    # 系统类
    "sudo", "su", "shutdown", "reboot", "init", "systemctl", "service",
    # 网络类（防止外传）
    "curl", "wget", "nc", "netcat", "ssh", "scp", "rsync",
    # 包管理
    "dnf", "yum", "apt", "pip install", "npm install",
    # 进程类
    "kill", "pkill", "killall",
}


def run_command(command: str) -> str:
    """执行终端命令（白名单 + 黑名单双重过滤）"""
    try:
        # 解析命令
        parts = shlex.split(command)
        if not parts:
            return "❌ 空命令"
        
        cmd_name = parts[0]
        full_cmd = " ".join(parts[:2]) if len(parts) >= 2 else cmd_name
        
        # 黑名单检查（优先级最高）
        if cmd_name in FORBIDDEN_COMMANDS:
            return f"❌ 禁止执行命令：{cmd_name}（黑名单）"
        
        # 白名单检查
        if full_cmd in ALLOWED_COMMANDS:
            pass  # 允许
        elif cmd_name in ALLOWED_COMMANDS:
            pass  # 允许
        else:
            return f"❌ 不允许执行命令：{command}（不在白名单）"
        
        # 执行命令（限制工作目录为当前目录）
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=os.getcwd(),
        )
        
        output = result.stdout or result.stderr
        if len(output) > 5000:
            output = output[:5000] + "\n... (输出太长，已截断)"
        
        return f"✅ 命令执行成功：\n{output}" if output else "✅ 命令执行成功（无输出）"
    except subprocess.TimeoutExpired:
        return "❌ 命令执行超时（超过 10 秒）"
    except Exception as e:
        return f"❌ 命令执行失败：{str(e)}"


# ============ 工具注册表 ============

TOOL_REGISTRY = {
    "get_weather": {
        "function": get_weather,
        "description": "查询指定城市的当前天气",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称，如：北京、上海"}
            },
            "required": ["city"]
        }
    },
    "get_current_time": {
        "function": get_current_time,
        "description": "获取当前日期和时间",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "description": "时区，默认为 Asia/Shanghai"}
            },
            "required": []
        }
    },
    "calculate": {
        "function": calculate,
        "description": "执行数学计算，支持加减乘除和括号",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "数学表达式，如 '2+3*4'"}
            },
            "required": ["expression"]
        }
    },
    "web_search": {
        "function": web_search,
        "description": "联网搜索最新信息。当用户问新闻、实时信息、你不知道的事情时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"}
            },
            "required": ["query"]
        }
    },
    "read_file": {
        "function": read_file,
        "description": "读取本地文件内容。当用户想查看文件内容、分析代码、读取文档时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径或文件名"}
            },
            "required": ["file_path"]
        }
    },
    "write_file": {
        "function": write_file,
        "description": "写入或覆盖文件内容。用于创建新文件、修改现有文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要写入的内容"}
            },
            "required": ["file_path", "content"]
        }
    },
    "append_to_file": {
        "function": append_to_file,
        "description": "追加内容到文件末尾。",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要追加的内容"}
            },
            "required": ["file_path", "content"]
        }
    },
    "run_command": {
        "function": run_command,
        "description": "执行终端命令（受白名单和黑名单限制）。用于查看文件、搜索内容、执行安全命令。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令，如 'ls -la'"}
            },
            "required": ["command"]
        }
    },
}


# ============ 工具管理函数 ============

def get_tools_schema():
    """生成 OpenAI 格式的 tools 列表"""
    tools = []
    for name, info in TOOL_REGISTRY.items():
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": info["description"],
                "parameters": info["parameters"]
            }
        })
    return tools


def execute_tool(tool_name: str, arguments: dict, request_id: str = None, task_id: str = None) -> str:
    """执行工具并返回结果（带追踪）"""
    if tool_name not in TOOL_REGISTRY:
        return f"错误：未知工具 '{tool_name}'"
    
    tool_func = TOOL_REGISTRY[tool_name]["function"]
    start = time.time()
    
    # 写文件类工具注入事务 task_id（用于写前快照）
    call_args = dict(arguments)
    if task_id and tool_name in ("write_file", "append_to_file"):
        call_args["_task_id"] = task_id
    
    try:
        result = tool_func(**call_args)
        duration_ms = (time.time() - start) * 1000
        track_tool_call(tool_name, arguments, duration_ms, True, request_id=request_id)
        return result
    except Exception as e:
        duration_ms = (time.time() - start) * 1000
        track_tool_call(tool_name, arguments, duration_ms, False, str(e), request_id=request_id)
        return f"工具执行错误: {str(e)}"
