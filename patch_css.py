#!/usr/bin/env python3
"""修复群发栏 CSS：1. 缩略图关闭按钮类名对齐 2. 发送按钮样式优化"""

with open('/app/static/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. 修改 .cb-thumb .x → .cb-thumb .cb-thumb-x（与 JS 代码一致）
content = content.replace(
    '.cb-thumb .x {',
    '.cb-thumb .cb-thumb-x {'
)

# 2. 发送按钮样式：改为正常尺寸，写「发送」文字
content = content.replace(
    '''            flex:0 0 auto; height:114px; padding:0 10px; width:34px; border-radius:10px;
            border:none; background:#3b82f6; color:#fff; font-size:12px;
            font-weight:600; cursor:pointer;''',
    '''            flex:0 0 auto; min-height:44px; padding:10px 22px; border-radius:10px;
            border:none; background:var(--accent); color:#fff; font-size:13px;
            font-weight:600; cursor:pointer; transition:background .18s;'''
)

# 3. 发送按钮 hover 效果（在 .cb-row button:disabled 前插入）
content = content.replace(
    '.cb-row button:disabled { opacity:.5; cursor:not-allowed; }',
    '.cb-row button:hover:not(:disabled) { background:var(--accent-2); }\n        .cb-row button:disabled { opacity:.5; cursor:not-allowed; }'
)

# 4. 在 addCard 里加 updateCbTarget() 调用
#    addCard 结尾是 renderModelList(); updateCounters(); }
content = content.replace(
    '        renderModelList();\n        updateCounters();\n    }\n\n    // 叉掉',
    '        renderModelList();\n        updateCounters();\n        updateCbTarget();\n    }\n\n    // 叉掉'
)

# 5. 在 closeCard 里加 updateCbTarget() 调用
#    closeCard 里 task.detached = true; 后面
content = content.replace(
    '        task.detached = true;',
    '        task.detached = true;\n        updateCbTarget();'
)

with open('/app/static/index.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("✅ CSS 和钩子修复完成")
