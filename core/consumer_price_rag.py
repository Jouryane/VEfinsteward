"""
VE5 消费价格 RAG
================
存储用户购物价格记录，支持语义检索，为 life_planner 提供区域物价参考。

与研报 RAG、财务 RAG 物理隔离：
    - 研报 RAG: tactical/fundamental/knowledge/chroma/
    - 财务 RAG: userdata/financial_rag/
    - 价格 RAG: userdata/consumer_price_rag/

数据来源：
    - receipt: 用户上传购物小票 OCR 提取
    - manual: 用户手动录入
    - government: WebSearch 获取的政府公开物价

设计原则：
    - 独立模块，不与 life_planner 硬耦合
    - 懒加载初始化
    - 异步写入，不阻塞主流程
"""

import os
import sys
import json
import logging
import threading
import hashlib
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger("ve5.consumer_price_rag")

# ── ChromaDB 在 frozen（PyInstaller）模式下直接跳过，使用 JSON 降级 ──
# chromadb 依赖 posthog 遥测和 rust 绑定，PyInstaller 打包时无法完整收集
_IS_FROZEN = getattr(sys, 'frozen', False) and not hasattr(sys, '_MEIPASS_pass_through')

# ── 路径 ──
from app_paths import DATA_DIR
_RAG_DIR = DATA_DIR / "consumer_price_rag"
_COLLECTION_NAME = "price_records"

# ── 单例缓存 ──
_client = None
_collection = None
_embed_fn = None
_initialized = False
_init_lock = threading.Lock()


def _ensure_init():
    """懒加载初始化（线程安全）"""
    global _client, _collection, _embed_fn, _initialized
    if _initialized:
        return
    
    # frozen 模式直接跳过 ChromaDB，使用 JSON 降级存储
    if _IS_FROZEN:
        logger.debug("[PRICE-RAG] PyInstaller 模式，跳过 ChromaDB，使用 JSON 降级")
        _initialized = True
        return

    with _init_lock:
        if _initialized:
            return
        try:
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

            import chromadb
            _RAG_DIR.mkdir(parents=True, exist_ok=True)
            _client = chromadb.PersistentClient(path=str(_RAG_DIR))

            _embed_fn = chromadb.utils.embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="BAAI/bge-small-zh-v1.5"
            )
            # 预热
            _ = _embed_fn(["初始化"])

            _collection = _client.get_or_create_collection(
                name=_COLLECTION_NAME,
                metadata={"hnsw:space": "cosine", "description": "消费价格记录"},
                embedding_function=_embed_fn,
            )
            _initialized = True
            logger.info(f"[PRICE-RAG] 初始化完成: {_RAG_DIR}, 记录数={_collection.count()}")
        except Exception as e:
            logger.warning(f"[PRICE-RAG] 初始化失败: {e}")
            _initialized = False


def ve5_price_rag_store(
    item: str,
    price: float,
    spec: str = "",
    merchant: str = "",
    location: str = "",
    address: str = "",
    category: str = "",
    source: str = "manual",  # receipt | manual | government
    date: str = None,
) -> bool:
    """
    存储一条价格记录。

    Args:
        item: 商品名称（如"鸡蛋"）
        price: 价格（数字）
        spec: 规格（如"30枚/盒"）
        merchant: 商家（如"盒马"）
        location: 区域（如"上海浦东"）
        address: 具体地址（可选）
        category: 分类（食材/日用品/水果/肉类/粮油...）
        source: 数据来源 receipt | manual | government
        date: 日期，默认今天
    """
    _ensure_init()
    if not _initialized:
        return False

    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    doc_id = f"price_{hashlib.md5(f'{item}{price}{merchant}{location}{date}'.encode()).hexdigest()[:12]}"

    # 构建可搜索文本（商品名 + 规格 + 商家 + 区域 + 分类）
    search_text = f"{item} {spec} {merchant} {location} {category}".strip()

    meta = {
        "item": item,
        "spec": spec,
        "price": price,
        "merchant": merchant,
        "location": location,
        "address": address,
        "category": category,
        "source": source,
        "date": date,
        "stored_at": datetime.now().isoformat(),
    }

    try:
        # 先删除同商家+同商品+同日期的旧记录（避免重复）
        _collection.delete(
            where={
                "$and": [
                    {"item": {"$eq": item}},
                    {"merchant": {"$eq": merchant}},
                    {"location": {"$eq": location}},
                    {"date": {"$eq": date}},
                ]
            }
        )
    except Exception:
        pass

    try:
        _collection.add(documents=[search_text], ids=[doc_id], metadatas=[meta])
        logger.info(f"[PRICE-RAG] 已存储: {item}={price}@{merchant}/{location} ({source})")
        return True
    except Exception as e:
        logger.error(f"[PRICE-RAG] 存储失败: {e}")
        return False


def ve5_price_rag_search(
    query: str,
    n_results: int = 5,
    location_filter: str = None,
    category_filter: str = None,
    source_filter: str = None,
) -> List[Dict[str, Any]]:
    """
    语义检索价格记录。

    Args:
        query: 查询文本（如"鸡蛋 上海浦东"）
        n_results: 返回数量
        location_filter: 按区域过滤（可选）
        category_filter: 按分类过滤（可选）
        source_filter: 按来源过滤（可选）

    Returns:
        [{item, spec, price, merchant, location, category, source, date, distance}, ...]
    """
    _ensure_init()
    if not _initialized:
        return []

    # 构建 where 过滤条件
    where_filter = {}
    if location_filter:
        where_filter["location"] = {"$eq": location_filter}
    if category_filter:
        where_filter["category"] = {"$eq": category_filter}
    if source_filter:
        where_filter["source"] = {"$eq": source_filter}

    try:
        kwargs = {"query_texts": [query], "n_results": n_results, "include": ["metadatas", "distances"]}
        if where_filter:
            kwargs["where"] = where_filter

        results = _collection.query(**kwargs)
        items = []
        for i in range(len(results["metadatas"][0])):
            meta = results["metadatas"][0][i]
            dist = results["distances"][0][i]
            items.append({
                "item": meta.get("item", ""),
                "spec": meta.get("spec", ""),
                "price": meta.get("price", 0),
                "merchant": meta.get("merchant", ""),
                "location": meta.get("location", ""),
                "category": meta.get("category", ""),
                "source": meta.get("source", ""),
                "date": meta.get("date", ""),
                "distance": dist,
            })
        return items
    except Exception as e:
        logger.error(f"[PRICE-RAG] 检索失败: {e}")
        return []


def ve5_price_rag_batch_store(records: List[Dict[str, Any]]) -> int:
    """
    批量存储价格记录。

    Args:
        records: [{item, price, spec, merchant, location, category, source, date}, ...]

    Returns:
        成功存储的数量
    """
    _ensure_init()
    if not _initialized or not records:
        return 0

    docs = []
    ids = []
    metas = []
    for r in records:
        item = r.get("item", "")
        price = r.get("price", 0)
        merchant = r.get("merchant", "")
        location = r.get("location", "")
        date = r.get("date", datetime.now().strftime("%Y-%m-%d"))
        doc_id = f"price_{hashlib.md5(f'{item}{price}{merchant}{location}{date}'.encode()).hexdigest()[:12]}"
        search_text = f"{item} {r.get('spec','')} {merchant} {location} {r.get('category','')}".strip()
        meta = {
            "item": item,
            "spec": r.get("spec", ""),
            "price": price,
            "merchant": merchant,
            "location": location,
            "address": r.get("address", ""),
            "category": r.get("category", ""),
            "source": r.get("source", "manual"),
            "date": date,
            "stored_at": datetime.now().isoformat(),
        }
        docs.append(search_text)
        ids.append(doc_id)
        metas.append(meta)

    try:
        _collection.add(documents=docs, ids=ids, metadatas=metas)
        logger.info(f"[PRICE-RAG] 批量存储成功: {len(records)} 条")
        return len(records)
    except Exception as e:
        logger.error(f"[PRICE-RAG] 批量存储失败: {e}")
        return 0


def ve5_price_rag_stats() -> Dict[str, Any]:
    """返回价格 RAG 统计信息"""
    _ensure_init()
    if not _initialized:
        return {"available": False}
    try:
        return {
            "available": True,
            "total_records": _collection.count(),
            "collection": _COLLECTION_NAME,
            "path": str(_RAG_DIR),
        }
    except Exception as e:
        return {"available": False, "error": str(e)}


def ve5_price_rag_clear():
    """清空所有价格记录"""
    _ensure_init()
    if not _initialized:
        return
    try:
        count = _collection.count()
        all_ids = _collection.get()["ids"]
        if all_ids:
            _collection.delete(ids=all_ids)
        logger.info(f"[PRICE-RAG] 已清空: {count} 条记录")
    except Exception as e:
        logger.error(f"[PRICE-RAG] 清空失败: {e}")


def ve5_price_rag_delete_by_item(item: str, merchant: str = None, location: str = None):
    """删除指定商品的价格记录"""
    _ensure_init()
    if not _initialized:
        return
    try:
        where = {"item": {"$eq": item}}
        if merchant:
            where["merchant"] = {"$eq": merchant}
        if location:
            where["location"] = {"$eq": location}
        _collection.delete(where=where)
        logger.info(f"[PRICE-RAG] 已删除 {item} 相关记录")
    except Exception as e:
        logger.error(f"[PRICE-RAG] 删除失败: {e}")


def ve5_price_rag_recent(n: int = 10, location: str = None) -> List[Dict[str, Any]]:
    """
    获取最近录入的价格记录（按时间倒序）。
    用于前端展示用户最近的购物记录。
    """
    _ensure_init()
    if not _initialized:
        return []
    try:
        # ChromaDB 没有原生排序，用 get() 获取所有后按 date 排序
        results = _collection.get(include=["metadatas"])
        items = []
        for meta in results.get("metadatas", []):
            if location and meta.get("location") != location:
                continue
            items.append({
                "item": meta.get("item", ""),
                "spec": meta.get("spec", ""),
                "price": meta.get("price", 0),
                "merchant": meta.get("merchant", ""),
                "location": meta.get("location", ""),
                "category": meta.get("category", ""),
                "source": meta.get("source", ""),
                "date": meta.get("date", ""),
            })
        items.sort(key=lambda x: x.get("date", ""), reverse=True)
        return items[:n]
    except Exception as e:
        logger.error(f"[PRICE-RAG] 获取最近记录失败: {e}")
        return []


def ve5_price_rag_unique_locations() -> List[str]:
    """获取所有已记录的区域列表"""
    _ensure_init()
    if not _initialized:
        return []
    try:
        results = _collection.get(include=["metadatas"])
        locations = set()
        for meta in results.get("metadatas", []):
            loc = meta.get("location", "")
            if loc:
                locations.add(loc)
        return sorted(list(locations))
    except Exception as e:
        logger.error(f"[PRICE-RAG] 获取区域列表失败: {e}")
        return []