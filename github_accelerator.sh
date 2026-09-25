#!/bin/bash
# ============================================================
# 🚀 GitHub 上传加速一键配置脚本
# ============================================================
# 用法: bash github_accelerator.sh [代理端口] [代理主机]
# 示例: bash github_accelerator.sh 7890
#        bash github_accelerator.sh 7890 172.17.0.1  (Docker内访问宿主机)
# ============================================================

set -e

PROXY_PORT="${1:-7890}"
PROXY_HOST="${2:-127.0.0.1}"
PROJECT_DIR="/app"
GIT_USER="${3:-zwh}"
GIT_EMAIL="${4:-${GIT_USER}@users.noreply.github.com}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✅ $1${NC}"; }
warn() { echo -e "  ${YELLOW}⚠️  $1${NC}"; }
fail() { echo -e "  ${RED}❌ $1${NC}"; }
info() { echo -e "  ${CYAN}💡 $1${NC}"; }

echo ""
echo "🚀 GitHub 上传加速一键配置"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "代理地址: http://${PROXY_HOST}:${PROXY_PORT}"
echo "项目目录: ${PROJECT_DIR}"
echo ""

# ──────────────────────────────────
# Step 1: 配置 Git 代理
# ──────────────────────────────────
echo "① 配置 Git 代理（仅 github.com 走代理）..."
git config --global http.https://github.com.proxy http://${PROXY_HOST}:${PROXY_PORT}
git config --global http.https://github.com.sslverify false
ok "http.https://github.com.proxy = http://${PROXY_HOST}:${PROXY_PORT}"

# ──────────────────────────────────
# Step 2: Git 用户信息
# ──────────────────────────────────
echo "② 配置 Git 用户信息..."
if [ -z "$(git config --global user.name 2>/dev/null)" ]; then
    git config --global user.name "${GIT_USER}"
    git config --global user.email "${GIT_EMAIL}"
    ok "用户: ${GIT_USER} <${GIT_EMAIL}>"
else
    ok "已有用户: $(git config --global user.name) <$(git config --global user.email)>"
fi

# ──────────────────────────────────
# Step 3: 增大 buffer（大文件推送）
# ──────────────────────────────────
echo "③ 增大 Git postBuffer（大文件场景）..."
git config --global http.postBuffer 524288000
ok "postBuffer = 524288000 (500MB)"

# ──────────────────────────────────
# Step 4: 初始化项目 Git 仓库
# ──────────────────────────────────
echo "④ 初始化项目 Git 仓库..."
cd ${PROJECT_DIR}

if [ ! -d ".git" ]; then
    git init
    ok "已初始化 Git 仓库"
else
    ok "已有 Git 仓库"
fi

# ──────────────────────────────────
# Step 5: 创建 .gitignore
# ──────────────────────────────────
echo "⑤ 生成 .gitignore..."
if [ ! -f ".gitignore" ]; then
    cat > .gitignore << 'GIEOF'
# Python
__pycache__/
*.pyc
*.pyo
*.pyd
.Python
*.egg-info/
dist/
build/

# 虚拟环境
.venv/
venv/
env/

# 备份文件
*.bak
*.bak2
*.bak3
*.bak_*
*.bak.*

# 事务临时文件
.txn/

# 数据目录（含 DB、约束词、运行时模型配置）
data/

# 日志
server.log
server_new.log
agent.log
*.log

# 数据库
*.db
*.db.bak_*
*.db.cleared_*

# 临时文件
_probe2.txt
_demo.txt.bak

# IDE
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db
GIEOF
    ok "已创建 .gitignore"
else
    ok "已有 .gitignore"
fi

# ──────────────────────────────────
# Step 6: 验证代理连通性
# ──────────────────────────────────
echo "⑥ 验证代理连通性..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    --proxy http://${PROXY_HOST}:${PROXY_PORT} \
    --connect-timeout 5 \
    https://github.com 2>/dev/null || echo "000")

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "301" ] || [ "$HTTP_CODE" = "302" ]; then
    ok "GitHub 连通 (HTTP ${HTTP_CODE})"
elif [ "$HTTP_CODE" = "000" ]; then
    fail "代理连接失败！请检查："
    info "1. 代理是否启动（Clash/V2Ray/Trojan）"
    info "2. 端口是否为 ${PROXY_PORT}"
    info "3. 容器内访问宿主机需用 172.17.0.1 而非 127.0.0.1"
    info "   bash github_accelerator.sh ${PROXY_PORT} 172.17.0.1"
else
    warn "GitHub 返回 HTTP ${HTTP_CODE}，代理可能有问题"
fi

# ──────────────────────────────────
# Step 7: 显示当前 remote
# ──────────────────────────────────
echo "⑦ 检查 Git remote..."
REMOTE=$(git remote get-url origin 2>/dev/null || echo "")
if [ -n "$REMOTE" ]; then
    ok "origin → ${REMOTE}"
else
    info "尚未设置 remote，推送前需要执行："
    echo "   git remote add origin https://github.com/<用户名>/<仓库名>.git"
fi

# ──────────────────────────────────
# 完成
# ──────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${GREEN}✅ 配置完成！${NC}"
echo ""
echo "📦 推送步骤："
echo "   cd ${PROJECT_DIR}"
echo "   git add ."
echo "   git commit -m 'feat: initial commit'"
if [ -z "$REMOTE" ]; then
    echo "   git remote add origin https://github.com/<用户名>/<仓库名>.git"
fi
echo "   git branch -M main"
echo "   git push -u origin main"
echo ""
echo "🔧 取消代理（不再需要时）："
echo "   git config --global --unset http.https://github.com.proxy"
echo "   git config --global --unset http.https://github.com.sslverify"
echo ""
echo "🧪 测试代理连通性："
echo "   curl --proxy http://${PROXY_HOST}:${PROXY_PORT} -I https://github.com"
