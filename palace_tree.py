# palace_tree.py
# 【记忆宫殿 · 话题分叉】按提问内容计算卡片父子关系（可跨对话语义挂靠）
# 数据源：memory.py 的 chat_cards（平台卡片对话）
# 规则（与前端兜底算法一致）：
#   1. 短句（<= SHORT_Q_LEN 字，如"继续""好的"）→ 挂同一对话上一张问题卡
#   2. 与任一更早问题卡余弦相似度 >= threshold → 挂该卡（跨对话亦可）
#   3. 都不满足 → 新开话题（分叉根）
# 嵌入：gt_all_minilm_l6_v2 离线模型；向量带内存缓存，重复打开宫殿不重算

import threading

# 阈值经真实数据扫描校准（26 卡）：0.55→2 话题 / 0.7→6 话题[14,5,2,2,2,1] / 0.8→13 话题
DEFAULT_THRESHOLD = 0.70
SHORT_Q_LEN = 10

_MODEL = None
_MODEL_LOCK = threading.Lock()
_EMB_CACHE = {}
_CACHE_LOCK = threading.Lock()


def warmup():
    """启动预热：提前加载嵌入模型，避免首次打开宫殿等待加载。"""
    try:
        _get_model()
    except Exception as e:
        print(f"⚠️ palace_tree warmup failed: {e}")


def _get_model():
    global _MODEL
    if _MODEL is None:
        with _MODEL_LOCK:
            if _MODEL is None:
                import gt_all_minilm_l6_v2
                _MODEL = gt_all_minilm_l6_v2.load_model()
    return _MODEL


def _embed_all(texts):
    """带缓存的批量嵌入，返回与 texts 等长的向量列表。"""
    need = []
    seen = set()
    for t in texts:
        if t not in _EMB_CACHE and t not in seen:
            seen.add(t)
            need.append(t)
    if need:
        model = _get_model()
        vecs = model.encode(need).tolist()
        with _CACHE_LOCK:
            for t, v in zip(need, vecs):
                _EMB_CACHE[t] = v
    return [_EMB_CACHE[t] for t in texts]


def _extract_cards(chats):
    """从 chat_cards 抽出问题卡。配对规则与前端 palBuildData 保持一致，uid 才能对上：
    user 提问占一卡；无等待提问的孤立 assistant 回复也占一卡（但无 q，不入树）。
    返回 [{uid, chat_id, round, q, t}]，t = 对话级 updated_at（跨对话排序的近似时间轴）。"""
    cards = []
    for chat in chats:
        chat_id = chat.get("chat_id") or ""
        rounds = []
        for m in chat.get("messages") or []:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = str(m.get("content") or "").strip()
            last = rounds[-1] if rounds else None
            if role == "user" and content:
                rounds.append({"round": len(rounds) + 1, "q": content, "pending": True})
            elif role == "assistant" and content:
                if last and last.get("pending"):
                    last["pending"] = False
                else:
                    rounds.append({"round": len(rounds) + 1, "q": "", "pending": False})
        t = chat.get("updated_at") or ""
        for r in rounds:
            if r["q"]:
                cards.append({
                    "uid": f"{chat_id}#{r['round']}",
                    "chat_id": chat_id,
                    "round": r["round"],
                    "q": r["q"],
                    "t": t,
                })
    # 全局时间序：对话 updated_at → chat_id → 轮次
    cards.sort(key=lambda c: (c["t"], c["chat_id"], c["round"]))
    return cards


def _assign_parents(cards, threshold):
    """按时间序给每张问题卡找父卡。返回 {uid: {'parent': uid|None, 'is_root': bool, 'sim': float, 'short': bool}}"""
    import numpy as np

    parents = {}
    prev_by_chat = {}
    if cards:
        vecs = _embed_all([c["q"] for c in cards])
        m = np.array(vecs, dtype="float32")
        n = np.linalg.norm(m, axis=1, keepdims=True)
        m = m / np.maximum(n, 1e-9)
        sim = m @ m.T

    for i, c in enumerate(cards):
        short = len(c["q"]) <= SHORT_Q_LEN
        parent = None
        best = 0.0
        if short and c["chat_id"] in prev_by_chat:
            parent = prev_by_chat[c["chat_id"]]      # 短追问默认延续本对话上一话题
        elif i > 0:
            j = int(np.argmax(sim[i, :i]))
            best = float(sim[i, j])
            if best >= threshold:
                parent = cards[j]["uid"]
        parents[c["uid"]] = {
            "parent": parent,
            "is_root": parent is None,
            "sim": round(best, 4) if parent else 1.0,
            "short": short,
        }
        prev_by_chat[c["chat_id"]] = c["uid"]
    return parents


def build_palace_tree(limit: int = 300, threshold: float = DEFAULT_THRESHOLD):
    """主入口：返回 {cards, topics, threshold, count}。
    cards:  [{uid, chat_id, round, parent, is_root, sim, short, topic}]
    topics: [{id(根卡uid), label, size}]（按根卡时间排序）"""
    import memory as memory_store

    threshold = DEFAULT_THRESHOLD if threshold is None else float(threshold)
    chats = memory_store.load_chat_cards(limit=max(1, min(int(limit), 500)))
    cards = _extract_cards(chats)
    parents = _assign_parents(cards, threshold)

    # 话题 = 沿 parent 链走到根；根卡即话题头
    info = {c["uid"]: c for c in cards}
    topics = {}
    out_cards = []
    for c in cards:
        meta = parents[c["uid"]]
        p = meta["parent"]
        root = c["uid"]
        hops = 0
        while p and p in info and hops < 50:
            root = p
            p = parents.get(p, {}).get("parent")
            hops += 1
        out_cards.append({
            "uid": c["uid"], "chat_id": c["chat_id"], "round": c["round"],
            "parent": meta["parent"], "is_root": meta["is_root"],
            "sim": meta["sim"], "short": meta["short"], "topic": root,
        })
        t = topics.get(root)
        if not t:
            topics[root] = {"id": root, "label": c["q"][:16], "size": 0,
                            "order": (c["t"], c["chat_id"], c["round"])}
        topics[root]["size"] += 1

    topic_list = sorted(topics.values(), key=lambda x: x["order"])
    for t in topic_list:
        t.pop("order", None)
    return {"cards": out_cards, "topics": topic_list, "threshold": threshold, "count": len(out_cards)}
