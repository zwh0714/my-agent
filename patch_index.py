#!/usr/bin/env python3
"""将群发功能代码块插入到 index.html 第1165行之后"""

# 读取原文件
with open('/app/static/index.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 读取要插入的代码块
with open('/app/broadcast_block.js', 'r', encoding='utf-8') as f:
    insert_block = f.read()

# 在第1165行之后插入（索引1165，即 lines[0:1165] + insert + lines[1165:]）
insert_pos = 1165  # 第1166行开始的位置（0-indexed = 1165）

new_lines = lines[:insert_pos] + [insert_block] + lines[insert_pos:]

# 写回
with open('/app/static/index.html', 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print(f"✅ 插入完成：原 {len(lines)} 行 → 新 {len(new_lines)} 行（+约{len(insert_block.split(chr(10)))}行群发功能代码）")
