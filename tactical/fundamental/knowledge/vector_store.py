"""
VE4/VE5 RAG 知识存储
====================
研报语义检索核心模块

双后端:
    1. ChromaDB 向量存储（推荐，需 chromadb + sentence-transformers）
    2. JSON 文件存储（降级方案，无需额外依赖）

Embedding:
    BAAI/bge-small-zh-v1.5 (384维, 中文优化)
    网络不可用时自动降级为纯文本存储

Chunk策略:
    chunk_size=500, overlap=100, 按段落优先分割
"""

import os
import sys
import re
import json
import logging
import threading
from pathlib import Path
from typing import List, Dict, Optional, Any
from datetime import datetime

logger = logging.getLogger("ve5.tactical.rag")

# ── 国内镜像加速 ──
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# ── 路径 ──
from app_paths import DATA_DIR, DB_PATH as _DB_PATH
CHROMA_DIR = DATA_DIR / "rag_chroma"
CHROMA_DIR.mkdir(parents=True, exist_ok=True)
JSON_STORE_PATH = DATA_DIR / "rag_reports.jsonl"

# ── 全局状态 ──
_chroma_available = None  # None=未测试, True=可用, False=不可用
_chroma_client = None
_collection = None
_embedder = None
_embedding_available = None  # None=未测试, True=可用, False=不可用
_json_lock = threading.Lock()


# ════════════════════════════════════════════════════════
# ChromaDB 后端
# ════════════════════════════════════════════════════════

def _test_chroma():
    """测试 ChromaDB 是否可用（导入 + 基本操作）"""
    global _chroma_available
    if _chroma_available is not None:
        return _chroma_available

    # frozen 模式直接跳过 ChromaDB，使用 JSON 降级存储
    # chromadb 依赖 posthog 遥测和 rust 绑定，PyInstaller 无法完整收集
    if getattr(sys, 'frozen', False):
        _chroma_available = False
        logger.debug("[RAG] PyInstaller 模式，跳过 ChromaDB，使用 JSON 文件存储")
        return _chroma_available

    # 开发模式：直接导入
    try:
        import chromadb
    except ImportError as e:
        err_msg = str(e).lower()
        if "posthog" in err_msg or "telemetry" in err_msg or "rust" in err_msg:
            # PyInstaller 打包时遗漏了 chromadb 子模块
            # 用带 Posthog 类的 mock 模块绕过
            # 注意：不要局部 import sys（会遮蔽模块级 import，导致 local variable 错误）
            from types import ModuleType

            def _make_posthog_mod():
                mod = ModuleType("chromadb.telemetry.product.posthog")
                class _Posthog:
                    def __init__(self, *a, **kw): pass
                    def capture(self, *a, **kw): pass
                    def identify(self, *a, **kw): pass
                    def close(self, *a, **kw): pass
                mod.Posthog = _Posthog
                return mod

            def _make_posthog_pkg():
                mod = ModuleType("posthog")
                mod.Posthog = type("Posthog", (), {
                    "__init__": lambda self, *a, **kw: None,
                    "capture": lambda self, *a, **kw: None,
                })
                return mod

            for _n, _f in [
                ("chromadb.telemetry.product.posthog", _make_posthog_mod),
                ("posthog", _make_posthog_pkg),
                ("chromadb.api.rust", lambda: ModuleType("chromadb.api.rust")),
            ]:
                if _n not in sys.modules:
                    sys.modules[_n] = _f()

            try:
                import chromadb
            except Exception as e2:
                _chroma_available = False
                logger.warning(f"[RAG] ChromaDB 不可用（mock后仍失败）: {e2}")
                return _chroma_available
        else:
            _chroma_available = False
            logger.warning(f"[RAG] ChromaDB 不可用: {e}")
            return _chroma_available

    # 第二次尝试：基本操作测试
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        col = client.get_or_create_collection(name="_test", metadata={"hnsw:space": "cosine"})
        col.delete(where={"_test": "1"})
        col.add(ids=["_t1"], documents=["test"], metadatas=[{"_test": "1"}])
        col.query(query_texts=["test"], n_results=1)
        col.delete(ids=["_t1"])
        client.delete_collection("_test")
        _chroma_available = True
        logger.info(f"[RAG] ChromaDB 可用: {CHROMA_DIR}")
    except Exception as e:
        _chroma_available = False
        logger.warning(f"[RAG] ChromaDB 不可用，降级为 JSON 文件存储: {e}")
    return _chroma_available


def _get_chroma_client():
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        _chroma_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        logger.info(f"[RAG] ChromaDB 初始化: {CHROMA_DIR}")
    return _chroma_client


def _get_collection():
    global _collection
    if _collection is None:
        client = _get_chroma_client()
        _collection = client.get_or_create_collection(
            name="report_knowledge",
            metadata={"hnsw:space": "cosine", "description": "研报知识库向量存储"}
        )
    return _collection


def _get_embedder():
    global _embedder, _embedding_available
    if _embedder is None and _embedding_available is not False:
        try:
            from sentence_transformers import SentenceTransformer
            model_name = "BAAI/bge-small-zh-v1.5"
            _embedder = SentenceTransformer(model_name)
            _embedding_available = True
            logger.info(f"[RAG] Embedding 模型加载: {model_name}")
        except Exception as e:
            _embedding_available = False
            logger.warning(f"[RAG] Embedding 模型加载失败，降级为纯文本模式: {e}")
    return _embedder


# ════════════════════════════════════════════════════════
# JSON 文件后端（降级方案）
# ════════════════════════════════════════════════════════

def _json_load_all() -> List[Dict]:
    """从 JSONL 文件加载所有研报"""
    if not JSON_STORE_PATH.exists():
        return []
    try:
        with open(JSON_STORE_PATH, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except Exception as e:
        logger.error(f"[RAG-JSON] 读取失败: {e}")
        return []


def _json_save_all(reports: List[Dict]):
    """将所有研报写入 JSONL 文件"""
    with _json_lock:
        with open(JSON_STORE_PATH, "w", encoding="utf-8") as f:
            for r in reports:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _json_upsert_report(report: Dict) -> bool:
    """JSON 后端：插入或更新研报"""
    reports = _json_load_all()
    report_id = report.get("report_id", "")
    # 删除旧版本
    reports = [r for r in reports if r.get("report_id") != report_id]
    # 追加新版本
    reports.append(report)
    _json_save_all(reports)
    logger.info(f"[RAG-JSON] 研报入库成功: {report_id}")
    return True


def _json_search(query: str, top_k: int = 5, report_id_filter: str = None,
                  time_decay_days: int = 180) -> List[Dict]:
    """JSON 后端：简单关键词匹配搜索"""
    reports = _json_load_all()
    if report_id_filter:
        reports = [r for r in reports if r.get("report_id") == report_id_filter]

    if not query or not query.strip():
        # 无查询词，按时间倒序返回
        reports.sort(key=lambda r: r.get("parsed_at", ""), reverse=True)
        return reports[:top_k]

    # 关键词匹配评分
    query_lower = query.lower()
    now = datetime.now()
    scored = []
    for r in reports:
        # 搜索范围：标题 + 摘要 + 投资逻辑 + 关键观点
        search_text = " ".join([
            r.get("title", ""),
            r.get("summary_text", ""),
            r.get("investment_thesis", ""),
            " ".join(r.get("key_points", [])),
        ]).lower()

        # 计算关键词命中数
        keywords = [k.strip() for k in query_lower if len(k.strip()) >= 2]
        hits = sum(1 for k in keywords if k in search_text)
        if hits == 0 and query_lower in search_text:
            hits = 1

        if hits == 0:
            continue

        # 时间衰减
        parsed_at_str = r.get("parsed_at", "")
        time_score = 1.0
        if parsed_at_str and time_decay_days > 0:
            try:
                parsed_dt = datetime.fromisoformat(parsed_at_str.replace("Z", "").replace("+00:00", ""))
                days_ago = (now - parsed_dt).total_seconds() / 86400
                if days_ago > 0:
                    time_score = max(0.0, 1.0 - days_ago / time_decay_days)
            except Exception:
                time_score = 0.5

        scored.append({
            "report_id": r.get("report_id", ""),
            "title": r.get("title", ""),
            "source_url": r.get("source_url", ""),
            "chunk_type": "report",
            "content": r.get("summary_text", "")[:500],
            "distance": 0.0,
            "time_score": round(time_score, 3),
            "final_score": round(min(1.0, hits / max(len(keywords), 1)) * 0.7 + time_score * 0.3, 3),
        })

    scored.sort(key=lambda x: x["final_score"], reverse=True)
    return scored[:top_k]


def _json_list_reports(limit: int = 50) -> List[Dict]:
    """JSON 后端：列出所有研报"""
    reports = _json_load_all()
    reports.sort(key=lambda r: r.get("parsed_at", ""), reverse=True)
    return [{
        "report_id": r.get("report_id", ""),
        "title": r.get("title", ""),
        "source_url": r.get("source_url", ""),
        "parsed_at": r.get("parsed_at", ""),
    } for r in reports[:limit]]


def _json_delete_report(report_id: str) -> bool:
    """JSON 后端：删除研报"""
    reports = _json_load_all()
    original_len = len(reports)
    reports = [r for r in reports if r.get("report_id") != report_id]
    if len(reports) < original_len:
        _json_save_all(reports)
        logger.info(f"[RAG-JSON] 研报删除成功: {report_id}")
        return True
    return False


# ════════════════════════════════════════════════════════
# 分块工具
# ════════════════════════════════════════════════════════

def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 100) -> List[str]:
    """按段落优先分块，保证语义连贯"""
    if not text:
        return []
    paragraphs = re.split(r'\n\s*\n|\n', text.strip())
    chunks = []
    current = ""
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if len(current) + len(p) + 1 <= chunk_size:
            current = (current + "\n" + p).strip() if current else p
        else:
            if current:
                chunks.append(current)
            if len(p) > chunk_size:
                sentences = re.split(r'[。！？；\n]', p)
                current = ""
                for s in sentences:
                    s = s.strip()
                    if not s:
                        continue
                    if len(current) + len(s) + 1 <= chunk_size:
                        current = (current + s + "。") if current else s + "。"
                    else:
                        if current:
                            chunks.append(current)
                        current = s + "。"
            else:
                current = p
    if current:
        chunks.append(current)

    if overlap > 0 and len(chunks) > 1:
        overlapped = []
        for i, chunk in enumerate(chunks):
            if i > 0:
                prev_tail = chunks[i - 1][-overlap:]
                chunk = prev_tail + chunk
            overlapped.append(chunk)
        chunks = overlapped

    return [c.strip() for c in chunks if len(c.strip()) > 10]


# ════════════════════════════════════════════════════════
# 统一接口（自动选择后端）
# ════════════════════════════════════════════════════════

def ve4_kb_upsert_report(report: Dict[str, Any]) -> bool:
    """
    将研报解析结果入库（覆盖式）
    report: {report_id, title, source_url, key_points, investment_thesis,
             risk_warnings, summary_text, parsed_at, full_text}
    """
    if not _test_chroma():
        # 降级到 JSON 文件
        return _json_upsert_report(report)

    try:
        report_id = report.get("report_id", f"report_{datetime.now().timestamp():.0f}")
        title = report.get("title", "未命名研报")
        source_url = report.get("source_url", "")
        parsed_at = report.get("parsed_at", datetime.now().isoformat())
        collection = _get_collection()

        # 先删除旧数据
        collection.delete(where={"report_id": report_id})

        # 构建待分块文本
        texts_to_chunk = []
        metas = []
        ids = []

        summary_text = report.get("summary_text", "")
        if summary_text:
            texts_to_chunk.append(f"【研报摘要】{title}\n{summary_text}")
            metas.append({"report_id": report_id, "title": title, "source_url": source_url,
                          "chunk_type": "summary", "parsed_at": parsed_at})
            ids.append(f"{report_id}_summary")

        for i, point in enumerate(report.get("key_points", [])[:10]):
            texts_to_chunk.append(f"【关键观点】{title}\n{point}")
            metas.append({"report_id": report_id, "title": title, "source_url": source_url,
                          "chunk_type": "key_point", "parsed_at": parsed_at})
            ids.append(f"{report_id}_kp_{i}")

        thesis = report.get("investment_thesis", "")
        if thesis and thesis != "未明确提取投资逻辑":
            texts_to_chunk.append(f"【投资逻辑】{title}\n{thesis}")
            metas.append({"report_id": report_id, "title": title, "source_url": source_url,
                          "chunk_type": "thesis", "parsed_at": parsed_at})
            ids.append(f"{report_id}_thesis")

        for i, risk in enumerate(report.get("risk_warnings", [])[:5]):
            texts_to_chunk.append(f"【风险提示】{title}\n{risk}")
            metas.append({"report_id": report_id, "title": title, "source_url": source_url,
                          "chunk_type": "risk", "parsed_at": parsed_at})
            ids.append(f"{report_id}_risk_{i}")

        full_text = report.get("full_text", summary_text)
        if full_text:
            chunks = _chunk_text(full_text, chunk_size=500, overlap=100)
            for i, chunk in enumerate(chunks):
                texts_to_chunk.append(f"【研报内容】{title}\n{chunk}")
                metas.append({"report_id": report_id, "title": title, "source_url": source_url,
                              "chunk_type": "full", "parsed_at": parsed_at})
                ids.append(f"{report_id}_full_{i}")

        if not texts_to_chunk:
            logger.warning(f"[RAG] 研报无内容可入库: {report_id}")
            return False

        # 生成 embedding 并入库（或纯文本 fallback）
        embedder = _get_embedder()
        if _embedding_available:
            embeddings = embedder.encode(texts_to_chunk, normalize_embeddings=True).tolist()
            collection.add(ids=ids, embeddings=embeddings, documents=texts_to_chunk, metadatas=metas)
        else:
            collection.add(ids=ids, documents=texts_to_chunk, metadatas=metas)

        logger.info(f"[RAG] 研报入库成功: {report_id}, {len(ids)} chunks")
        return True

    except Exception as e:
        logger.error(f"[RAG] ChromaDB 入库失败，降级到 JSON: {e}")
        # ChromaDB 失败时降级到 JSON
        return _json_upsert_report(report)


def ve4_kb_search(query: str, top_k: int = 5, report_id_filter: str = None,
                  time_decay_days: int = 180) -> List[Dict]:
    """语义/关键词检索研报片段"""
    if not _test_chroma():
        return _json_search(query, top_k, report_id_filter, time_decay_days)

    try:
        if not query or not query.strip():
            return []

        where_filter = {"report_id": report_id_filter} if report_id_filter else None
        fetch_k = min(top_k * 5, 50)
        collection = _get_collection()
        embedder = _get_embedder()

        if _embedding_available:
            query_embedding = embedder.encode([query], normalize_embeddings=True).tolist()
            results = collection.query(
                query_embeddings=query_embedding, n_results=fetch_k,
                where=where_filter, include=["documents", "metadatas", "distances"]
            )
        else:
            results = collection.query(
                query_texts=[query], n_results=fetch_k,
                where=where_filter, include=["documents", "metadatas", "distances"]
            )

        items = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        now = datetime.now()

        for i in range(len(ids)):
            meta = metas[i] if i < len(metas) else {}
            raw_distance = dists[i] if i < len(dists) else 1.0
            semantic_score = max(0.0, 1.0 - raw_distance)

            parsed_at_str = meta.get("parsed_at", "")
            time_score = 1.0
            if parsed_at_str and time_decay_days > 0:
                try:
                    parsed_dt = datetime.fromisoformat(parsed_at_str.replace("Z", "+00:00").replace("+00:00", ""))
                    days_ago = (now - parsed_dt).total_seconds() / 86400
                    if days_ago > 0:
                        time_score = max(0.0, 1.0 - days_ago / time_decay_days)
                except Exception:
                    time_score = 0.5

            final_score = semantic_score * 0.7 + time_score * 0.3

            items.append({
                "id": ids[i],
                "report_id": meta.get("report_id", ""),
                "title": meta.get("title", ""),
                "source_url": meta.get("source_url", ""),
                "chunk_type": meta.get("chunk_type", ""),
                "parsed_at": parsed_at_str,
                "content": docs[i] if i < len(docs) else "",
                "distance": raw_distance,
                "time_score": round(time_score, 3),
                "final_score": round(final_score, 3),
            })

        items.sort(key=lambda x: x["final_score"], reverse=True)
        return items[:top_k]

    except Exception as e:
        logger.error(f"[RAG] ChromaDB 检索失败，降级到 JSON: {e}")
        return _json_search(query, top_k, report_id_filter, time_decay_days)


def ve4_kb_list(limit: int = 50) -> List[Dict]:
    """列出所有研报（去重）"""
    if not _test_chroma():
        return _json_list_reports(limit)

    try:
        collection = _get_collection()
        results = collection.get(limit=min(limit * 5, 500), include=["metadatas"])
        seen = set()
        reports = []
        for m in results.get("metadatas", []):
            rid = m.get("report_id")
            if rid and rid not in seen:
                seen.add(rid)
                reports.append({
                    "report_id": rid,
                    "title": m.get("title", ""),
                    "source_url": m.get("source_url", ""),
                    "parsed_at": m.get("parsed_at", ""),
                })
        return reports[:limit]
    except Exception as e:
        logger.error(f"[RAG] ChromaDB 列举失败，降级到 JSON: {e}")
        return _json_list_reports(limit)


def ve4_kb_delete(report_id: str) -> bool:
    """删除研报"""
    if not _test_chroma():
        return _json_delete_report(report_id)

    try:
        collection = _get_collection()
        collection.delete(where={"report_id": report_id})
        logger.info(f"[RAG] 研报删除成功: {report_id}")
        return True
    except Exception as e:
        logger.error(f"[RAG] ChromaDB 删除失败，降级到 JSON: {e}")
        return _json_delete_report(report_id)


# ── 兼容旧代码的类（保留但内部委托到模块函数）──
class VE4RAGVectorStore:
    """研报向量存储（兼容旧接口，内部委托到模块级函数）"""
    def __init__(self):
        self.embedding_available = _embedding_available if _embedding_available is not None else False
        # 尝试初始化 embedding
        if _test_chroma():
            try:
                _get_embedder()
                self.embedding_available = _embedding_available
            except Exception:
                pass