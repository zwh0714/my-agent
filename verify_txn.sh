#!/bin/sh
# 事务回滚功能自检（重启后运行：sh verify_txn.sh）
python3 - <<'PY'
import urllib.request, json
B="http://localhost:8000"
def call(method, path, body=None):
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(B+path, data=data, method=method,
                                 headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, json.loads(r.read().decode())
    except Exception as e:
        return "ERR", str(e)

print("1) /api        ", call("GET","/api"))
print("2) /txn/list   ", call("GET","/txn/list?task_id=selftest"))
print("3) /txn/commit ", call("POST","/txn/commit", {"user_id":"x","task_id":"selftest"}))
print("4) /txn/rollback", call("POST","/txn/rollback", {"user_id":"x","task_id":"selftest"}))
print("（4 对不存在的事务应返回 ok:False/error，属正常）")
PY
