# history_vector.py
# 必须在 import chromadb 之前替换 sqlite3
__import__('pysqlite3')
import sys
sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')

import os
import chromadb
from chromadb.api.types import EmbeddingFunction
import gt_all_minilm_l6_v2


class CustomEmbeddingFunction(EmbeddingFunction):
    """自定义嵌入函数，包装离线模型"""
    def __init__(self, model):
        self.model = model

    def __call__(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        return self.model.encode(texts).tolist()

    def name(self):
        return "custom_offline_model"


class HistoryVectorStore:
    """历史对话的向量存储"""

    def __init__(self, persist_dir="./data/history_db"):
        os.makedirs(persist_dir, exist_ok=True)
        model = gt_all_minilm_l6_v2.load_model()
        self.embedding_fn = CustomEmbeddingFunction(model)
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self.client.get_or_create_collection(
            name="conversation_history",
            embedding_function=self.embedding_fn
        )

    def add_message(self, user_id: str, message_id: str, role: str, content: str):
        """添加一条消息到向量库"""
        if not content.strip():
            return
        if role not in ("user", "assistant"):
            return
        try:
            self.collection.add(
                documents=[content],
                ids=[f"{user_id}_{message_id}"],
                metadatas=[{"user_id": user_id, "role": role, "message_id": message_id}]
            )
        except Exception as e:
            print(f"⚠️ 向量库添加失败: {e}")

    def search_relevant(self, user_id: str, query: str, top_k: int = 5) -> list:
        """检索和当前问题最相关的历史消息"""
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=top_k,
                where={"user_id": user_id}
            )
            if results and results['documents'] and results['documents'][0]:
                return [
                    {"role": meta["role"], "content": doc}
                    for doc, meta in zip(results['documents'][0], results['metadatas'][0])
                ]
            return []
        except Exception as e:
            print(f"⚠️ 向量检索失败: {e}")
            return []

    def clear_user_history(self, user_id: str):
        """清空某个用户的历史"""
        try:
            self.collection.delete(where={"user_id": user_id})
        except Exception as e:
            print(f"⚠️ 清空历史失败: {e}")
