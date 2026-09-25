# 🚀 GitHub 上传加速完整方案

> 适用环境：Linux（Debian/Ubuntu/CentOS）/ macOS / WSL
> 生成时间：2025-09-25
> 本项目环境：Debian 13 (trixie) 容器

---

## 📋 方案总览

| 方案 | 适用场景 | 加速效果 | 难度 | 推荐度 |
|------|---------|---------|------|--------|
| ① Git HTTP/HTTPS 代理 | 有梯子/代理 | ⭐⭐⭐⭐⭐ | ⭐ | 👑首选 |
| ② Git SSH + ProxyCommand | 有梯子+SSH密钥 | ⭐⭐⭐⭐⭐ | ⭐⭐ | 🔒最稳 |
| ③ GitHub 镜像站中转 | 无代理 | ⭐⭐⭐ | ⭐⭐ | 🌐备选 |
| ④ hosts 加速 | 无代理 | ⭐⭐ | ⭐⭐ | 💊轻量 |
| ⑤ dev-sidecar 边车 | 桌面端 | ⭐⭐⭐⭐ | ⭐ | 🖥️GUI |

---

## 方案①：Git HTTP/HTTPS 代理（最推荐）

**前提**：你本地有一个代理（Clash/V2Ray/Trojan 等），假设监听在 `127.0.0.1:7890`

### 全局设置（所有 git 仓库生效）

```bash
# 设置 HTTP 代理
git config --global http.proxy http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890

# 验证
git config --global --get http.proxy
git config --global --get https.proxy

# 测试 push
git push origin main
```

### 仅对 GitHub 设置代理（不影响内网 GitLab）

```bash
# 仅 github.com 走代理
git config --global http.https://github.com.proxy http://127.0.0.1:7890

# 验证
git config --global --get http.https://github.com.proxy
```

### Socks5 代理（如果代理是 socks5 协议）

```bash
git config --global http.proxy socks5://127.0.0.1:7891
git config --global https.proxy socks5://127.0.0.1:7891
```

### 单项目设置（只对某个仓库生效）

```bash
cd /path/to/your/project

# 方法1：命令行
git config http.proxy http://127.0.0.1:7890
git config https.proxy http://127.0.0.1:7890

# 方法2：直接编辑 .git/config，在末尾添加
cat >> .git/config << 'EOF'
[http]
    proxy = http://127.0.0.1:7890
[https]
    proxy = http://127.0.0.1:7890
EOF
```

### 取消代理

```bash
# 全局取消
git config --global --unset http.proxy
git config --global --unset https.proxy

# 仅取消 GitHub 代理
git config --global --unset http.https://github.com.proxy
```

---

## 方案②：Git SSH + ProxyCommand（最稳定）

**前提**：已配置 SSH 密钥 + 有代理 + 安装了 `nc`（netcat）

### 安装依赖

```bash
# Debian/Ubuntu
apt install -y netcat-openbsd

# CentOS/RHEL
yum install -y nmap-ncat

# macOS（自带）
brew install netcat
```

### 配置 ~/.ssh/config

```bash
cat >> ~/.ssh/config << 'EOF'
Host github.com
    User git
    Hostname github.com
    Port 22
    # HTTP 代理（Clash 等）
    ProxyCommand nc -X connect -x 127.0.0.1:7890 %h %p

    # 如果是 Socks5 代理，改用：
    # ProxyCommand nc -X 5 -x 127.0.0.1:7891 %h %p
EOF

chmod 600 ~/.ssh/config
```

### 验证

```bash
# 测试 SSH 连接
ssh -T git@github.com
# 预期输出: Hi <username>! You've successfully authenticated...

# 之后 git push 自动走代理
git push origin main
```

### 🐳 Docker/容器环境特殊处理

容器内通常没有 `nc`，用 Python 替代：

```bash
# 创建一个 Python 版 ProxyCommand 脚本
cat > /usr/local/bin/ssh-proxy-helper << 'PYEOF'
#!/usr/bin/env python3
"""SSH ProxyCommand helper: 通过 HTTP CONNECT 代理转发 SSH 流量"""
import sys, socket

proxy_host, proxy_port = "127.0.0.1", 7890
target_host, target_port = sys.argv[1], int(sys.argv[2])

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect((proxy_host, proxy_port))
s.sendall(f"CONNECT {target_host}:{target_port} HTTP/1.1\r\nHost: {target_host}:{target_port}\r\n\r\n".encode())

response = b""
while b"\r\n\r\n" not in response:
    response += s.recv(4096)

if b"200" not in response.split(b"\r\n")[0]:
    sys.exit(1)

import select, os
while True:
    r, _, _ = select.select([s, sys.stdin], [], [], 1.0)
    if s in r:
        data = s.recv(4096)
        if not data: break
        os.write(sys.stdout.fileno(), data)
    if sys.stdin in r:
        data = os.read(sys.stdin.fileno(), 4096)
        if not data: break
        s.sendall(data)
PYEOF

chmod +x /usr/local/bin/ssh-proxy-helper
```

然后在 `~/.ssh/config` 里：

```
Host github.com
    User git
    Hostname github.com
    ProxyCommand /usr/local/bin/ssh-proxy-helper %h %p
```

---

## 方案③：GitHub 镜像站中转（无代理可用）

### 3a. 使用 ghfast.top 加速 clone

```bash
# 原始地址
git clone https://github.com/user/repo.git

# 加速地址（替换域名即可）
git clone https://ghfast.top/https://github.com/user/repo.git
```

### 3b. 使用 kkgithub 加速

```bash
# 把 github.com 换成 kkgithub.com
git clone https://kkgithub.com/user/repo.git
```

### 3c. 推送时切回原始地址

⚠️ **镜像站只能加速 clone，push 仍需走原站！**

```bash
# clone 时用镜像
git clone https://kkgithub.com/user/repo.git
cd repo

# push 前把 remote 改回原站
git remote set-url origin https://github.com/user/repo.git

# push（此时可能慢，需配合方案①或②加速 push）
git push origin main
```

### 3d. Gitee 中转方案（完全国内中转）

```bash
# 1. 在 Gitee 上 fork 你的 GitHub 仓库（或创建同名仓库）
# 2. 添加 Gitee remote
git remote add gitee https://gitee.com/yourname/repo.git

# 3. 先推到 Gitee（国内，秒传）
git push gitee main

# 4. 在 Gitee 仓库设置 → 同步管理 → 一键同步到 GitHub
#    （Gitee 服务器在美国有节点，同步 GitHub 很快）

# 或者反过来：先从 Gitee pull，再 push 到 GitHub
```

---

## 方案④：修改 hosts 文件（轻量级）

### 自动获取最快 IP

```bash
# 查询 GitHub 域名的最优 IP
# 需先安装：pip install github-hosts
pip3 install github-hosts

# 自动生成 hosts 条目
python3 -m github_hosts > /tmp/github_hosts

# 查看结果
cat /tmp/github_hosts
```

### 手动写入 /etc/hosts

```bash
# 2025年实测可用的 IP（需定期更新，以下为示例）
cat >> /etc/hosts << 'EOF'
# GitHub 加速
140.82.121.3    github.com
140.82.121.9    assets-cdn.github.com
185.199.108.133 raw.githubusercontent.com
185.199.109.133 raw.githubusercontent.com
185.199.110.133 raw.githubusercontent.com
185.199.111.133 raw.githubusercontent.com
185.199.108.154 github.githubassets.com
EOF
```

### 使用 DNS 工具自动更新

```bash
# 推荐使用 Watt-Toolkit（原 Steam++）的 GitHub 加速功能
# 或使用在线工具：https://github.com/521xueweihan/GitHub520
# 自动更新 hosts 的脚本：

curl -s https://raw.githubusercontent.com/521xueweihan/GitHub520/main/hosts >> /etc/hosts
```

---

## 方案⑤：dev-sidecar（桌面端一键加速）

### 安装

```
下载地址：https://github.com/docmirror/dev-sidecar/releases
```

### 功能

- GitHub 直连加速（修改 SNI）
- Release / Source / Zip 下载加速
- git clone / push 加速
- 头像 / Gist 加速
- 解决 README 图片加载

### 使用

1. 安装 → 启动 → 默认模式即可
2. 开着它，正常 git push 就行
3. ⚠️ Windows 下重启电脑后可能需重新打开

---

## 🔧 一键配置脚本（针对本项目 /app）

```bash
#!/bin/bash
# github_accelerator.sh - 一键为 /app 项目配置 GitHub 上传加速
# 用法: bash github_accelerator.sh [代理端口]
# 示例: bash github_accelerator.sh 7890

PROXY_PORT="${1:-7890}"
PROXY_HOST="127.0.0.1"
PROJECT_DIR="/app"

echo "🚀 GitHub 上传加速一键配置"
echo "============================="
echo "代理地址: http://${PROXY_HOST}:${PROXY_PORT}"
echo ""

# 1. 配置 Git 代理
echo "① 配置 Git 全局代理（仅 GitHub）..."
git config --global http.https://github.com.proxy http://${PROXY_HOST}:${PROXY_PORT}
git config --global http.https://github.com.sslverify false
echo "   ✅ 已设置: http.https://github.com.proxy = http://${PROXY_HOST}:${PROXY_PORT}"

# 2. 配置 Git 用户信息（如果未设置）
if [ -z "$(git config --global user.name)" ]; then
    git config --global user.name "zwh"
    git config --global user.email "zwh@users.noreply.github.com"
    echo "   ✅ 已设置 Git 用户: zwh"
fi

# 3. 初始化项目 Git 仓库
cd ${PROJECT_DIR}
if [ ! -d ".git" ]; then
    git init
    echo "   ✅ 已初始化 Git 仓库"
fi

# 4. 创建 .gitignore
if [ ! -f ".gitignore" ]; then
    cat > .gitignore << 'GIEOF'
__pycache__/
*.pyc
*.pyo
.venv/
*.bak
*.bak2
*.bak3
*.bak_*
*.bak.*
.txn/
data/
server.log
server_new.log
agent.log
*.db
*.db.bak_*
Demo.txt.bak
_probe2.txt
GIEOF
    echo "   ✅ 已创建 .gitignore"
fi

# 5. 验证连接
echo ""
echo "② 验证 GitHub 连接..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --proxy http://${PROXY_HOST}:${PROXY_PORT} https://github.com 2>/dev/null)
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "301" ]; then
    echo "   ✅ GitHub 连接正常 (HTTP ${HTTP_CODE})"
else
    echo "   ⚠️  GitHub 连接异常 (HTTP ${HTTP_CODE})，请检查代理是否启动"
    echo "   💡 提示: 确保 Clash/V2Ray 等代理正在运行，端口为 ${PROXY_PORT}"
fi

echo ""
echo "============================="
echo "✅ 配置完成！"
echo ""
echo "📦 推送步骤："
echo "   cd ${PROJECT_DIR}"
echo "   git add ."
echo "   git commit -m 'Initial commit'"
echo "   git remote add origin https://github.com/<你的用户名>/<仓库名>.git"
echo "   git push -u origin main"
echo ""
echo "🔧 取消代理："
echo "   git config --global --unset http.https://github.com.proxy"
```

---

## 🎯 快速决策树

```
你有代理（梯子）吗？
├── YES → 你用 HTTPS 还是 SSH？
│   ├── HTTPS → 方案①（2 行命令搞定）
│   └── SSH   → 方案②（最稳定，推荐长期用）
└── NO → 你有 Gitee 账号吗？
    ├── YES → 方案③ Gitee 中转（完全国内）
    └── NO  → 方案④ hosts 加速（聊胜于无）
              或方案⑤ dev-sidecar（桌面端）
```

---

## ⚡ 极速版（30秒搞定）

如果你有代理在 `127.0.0.1:7890`，只需这两行：

```bash
git config --global http.https://github.com.proxy http://127.0.0.1:7890
git push origin main   # 从此飞快 🚀
```

---

## 🐛 常见问题

### Q: push 报错 `Failed to connect to 127.0.0.1:7890`
A: 代理没启动或端口不对。检查：`curl --proxy http://127.0.0.1:7890 https://github.com`

### Q: push 报错 `OpenSSL SSL_connect: Connection was reset`
A: 添加 `git config --global http.sslverify false`（代理有自签证书时）

### Q: 想临时关代理
A: `git config --global --unset http.https://github.com.proxy`

### Q: push 大文件超时
A: 增加 buffer：`git config --global http.postBuffer 524288000`（500MB）

### Q: 容器内无法访问宿主机代理
A: 把 `127.0.0.1` 改成宿主机 IP，或 Docker 用 `--network host`：
```bash
git config --global http.https://github.com.proxy http://172.17.0.1:7890
```
