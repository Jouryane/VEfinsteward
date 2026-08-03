"""
VE5 Confidence Feedback Engine — 核验官
=========================================
实现用户描述的 confidence 核验流程：
  1. 经验被触发执行（execute）→ 自动检测成功/失败 → 更新 confidence
  2. LLM 完整输出后（delegate）→ 运行经验 executor → 对比 LLM 输出与经验输出 → 更新 confidence
  3. assist 模式 → 对比 LLM 微调结果与经验 prefill → 更新 confidence

核验公式遵循 V1 设计文档:
  Score = PA × (0.50 + 0.20×UF + 0.15×UA + 0.15×R) × DS

核心思路: confidence 的唯一职责是决定是否绕过 LLM。
  - 经验输出与 LLM 输出一致 → 经验可靠 → 提升 confidence
  - 经验输出与 LLM 输出差异大 → 经验不可靠 → 降低 confidence
  - 经验直接执行成功 → 提升 confidence
  - 经验直接执行失败 → 降低 confidence
"""

import json
import logging
import re
import math
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

logger = logging.getLogger("ve5.confidence_feedback")


# ════════════════════════════════════════════════
# 主入口: 统一反馈记录
# ════════════════════════════════════════════════

def record_feedback(
    exp_id: str,
    decision: str,
    exp_output: Optional[Dict] = None,
    llm_output: Optional[str] = None,
    llm_data: Optional[Dict] = None,
    success: Optional[bool] = None,
    context: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    统一的 confidence 反馈入口，在 chatbot 三条路径完成后调用。

    三种场景:
      1. decision="execute": 经验直接执行 → 自动检测 → 更新 confidence
      2. decision="assist": LLM 辅助 → 对比 LLM 输出与经验 prefill → 更新 confidence
      3. decision="delegate": LLM 完整输出 → 运行经验 executor → 对比 → 更新 confidence

    参数:
        exp_id: 经验 ID
        decision: "execute" | "assist" | "delegate"
        exp_output: 经验 executor 的输出 dict
        llm_output: LLM 的 reply 文本（delegate/assist 路径）
        llm_data: LLM 的结构化数据（delegate/assist 路径）
        success: 显式传入成功/失败（可选，覆盖自动检测）
        context: 上下文信息

    返回:
        {
            "exp_id": str,
            "success": bool,
            "similarity": float,  # LLM-vs-Experience 相似度
            "confidence_before": float,
            "confidence_after": float,
            "level_changed": bool,
        }
    """
    from core.experience_store import (
        exp_get, _compute_confidence_v1, _estimate_data_stability,
        _level_from_confidence, _get_conn,
    )

    exp = exp_get(exp_id)
    if not exp:
        logger.warning(f"[CONF_FEEDBACK] 经验不存在: {exp_id}")
        return {"exp_id": exp_id, "success": False, "error": "经验不存在"}

    old_conf = exp.get("confidence", 0.25)
    old_level = exp.get("level", "raw")

    # ── 确定 success ──
    detected_success = success
    similarity = 0.0

    if detected_success is None:
        if decision == "execute":
            # 场景 1: 经验直接执行 → 自动检测
            detected_success = _auto_detect_success(exp_output, exp)
            logger.debug(f"[CONF_FEEDBACK] execute 自动检测: {exp_id} → {'success' if detected_success else 'failure'}")

        elif decision == "assist":
            # 场景 2: assist 模式 → 对比 LLM 微调与经验 prefill
            exp_text = _extract_exp_text(exp_output)
            similarity = compare_outputs(llm_output or "", exp_text, exp)
            # 相似度高 → 经验预填被采纳 → success
            # 相似度低 → LLM 大幅修改 → 经验不够准确 → partial failure
            if similarity >= 0.6:
                detected_success = True
            elif similarity < 0.3:
                detected_success = False
            else:
                detected_success = None  # 不确定，不更新计数
            logger.debug(f"[CONF_FEEDBACK] assist 对比: {exp_id} sim={similarity:.3f} → {'success' if detected_success == True else 'failure' if detected_success == False else 'neutral'}")

        elif decision == "delegate":
            # 场景 3: LLM 完整输出 → 对比
            exp_text = _extract_exp_text(exp_output)
            similarity = compare_outputs(llm_output or "", exp_text, exp)
            # delegate 路径: 如果 LLM 输出与经验输出高度一致
            # → 说明经验本可以处理这个场景 → 提升 confidence
            if similarity >= 0.7:
                detected_success = True
            elif similarity < 0.2:
                # 差异极大 → 经验不适用于此场景 → 轻微降低
                detected_success = False
            else:
                # 中间区域: 不确定，不更新计数但记录激活
                detected_success = None
            logger.debug(f"[CONF_FEEDBACK] delegate 对比: {exp_id} sim={similarity:.3f} → {'success' if detected_success == True else 'failure' if detected_success == False else 'neutral'}")

    # ── 更新统计 + 重算 confidence ──
    now = datetime.now().isoformat()
    freq = exp.get("frequency", 0) + 1
    total = exp.get("total_usage", 0) + 1
    succ = exp.get("success_count", 0)
    fail = exp.get("failure_count", 0)

    if detected_success is True:
        succ += 1
    elif detected_success is False:
        fail += 1

    # 数据稳定性
    ds = _estimate_data_stability(exp_id)

    # V1 Confidence 重算
    new_conf, pa, uf, r_decay, ds_val = _compute_confidence_v1(
        success_count=succ,
        failure_count=fail,
        frequency=freq,
        positive_fb=succ,
        negative_fb=fail,
        last_used=now,
        data_stability=ds,
    )
    new_level = _level_from_confidence(new_conf)
    new_llm = 0 if new_level == "automatic" else 1

    # ── 写入数据库 ──
    conn = _get_conn()
    try:
        conn.execute("""
            UPDATE exp_experiences SET
                confidence = ?, prediction_accuracy = ?,
                frequency = ?, total_usage = ?,
                success_count = ?, failure_count = ?,
                last_used = ?, llm_required = ?, updated_at = ?
            WHERE exp_id = ?
        """, (new_conf, pa, freq, total, succ, fail, now, new_llm, now, exp_id))

        # 记录激活
        score_breakdown = {
            "pa": pa, "uf": uf, "r": r_decay, "ds": ds_val,
            "score": new_conf,
            "decision": decision,
            "similarity": round(similarity, 4),
            "auto_detected": success is None,
        }
        conn.execute("""
            INSERT INTO exp_activations
            (exp_id, triggered_by, score_breakdown, confidence_before, confidence_after,
             result_success, output_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            exp_id,
            (context or {}).get("triggered_by", decision),
            json.dumps(score_breakdown, ensure_ascii=False),
            old_conf, new_conf,
            1 if detected_success else (0 if detected_success is False else -1),
            json.dumps({
                "exp_reply": _extract_exp_text(exp_output)[:500] if exp_output else "",
                "llm_reply": (llm_output or "")[:500],
            }, ensure_ascii=False),
            now,
        ))
        conn.commit()
    finally:
        conn.close()

    level_changed = old_level != new_level
    if level_changed:
        if new_level == "automatic":
            logger.info(
                f"[CONF_FEEDBACK] ⚡ 升级: {exp_id} ({exp.get('name', '?')}) "
                f"{old_level} → {new_level} (conf={new_conf:.3f}, sim={similarity:.3f})"
            )
        else:
            logger.info(
                f"[CONF_FEEDBACK] 退化: {exp_id} ({exp.get('name', '?')}) "
                f"{old_level} → {new_level} (conf={new_conf:.3f})"
            )

    return {
        "exp_id": exp_id,
        "success": detected_success,
        "similarity": round(similarity, 4),
        "confidence_before": old_conf,
        "confidence_after": new_conf,
        "level_changed": level_changed,
        "new_level": new_level,
    }


# ════════════════════════════════════════════════
# LLM-vs-Experience 输出对比
# ════════════════════════════════════════════════

def compare_outputs(
    llm_output: str,
    exp_output_text: str,
    experience: Optional[Dict] = None,
) -> float:
    """
    对比 LLM 输出与经验输出的相似度。

    返回 0.0-1.0:
      1.0 = 完全一致（经验可靠，本可绕过 LLM）
      0.0 = 完全不同（经验不适用）

    对比维度:
      1. 数值一致性 (40%): 关键金额/百分比是否一致
      2. 关键词重叠 (30%): 主题词汇重叠度
      3. 结构相似度 (30%): 内容结构和段落组织
    """
    if not llm_output or not exp_output_text:
        return 0.0

    llm_clean = _clean_text(llm_output)
    exp_clean = _clean_text(exp_output_text)

    if not llm_clean or not exp_clean:
        return 0.0

    # ── 维度 1: 数值一致性 (40%) ──
    llm_nums = _extract_numbers(llm_clean)
    exp_nums = _extract_numbers(exp_clean)
    num_score = _compare_numbers(llm_nums, exp_nums)

    # ── 维度 2: 关键词重叠 (30%) ──
    llm_words = set(_tokenize(llm_clean))
    exp_words = set(_tokenize(exp_clean))
    if llm_words and exp_words:
        overlap = len(llm_words & exp_words) / len(llm_words | exp_words)
    else:
        overlap = 0.0
    keyword_score = overlap

    # ── 维度 3: 结构相似度 (30%) ──
    llm_seps = _extract_structure(llm_clean)
    exp_seps = _extract_structure(exp_clean)
    if llm_seps and exp_seps:
        common_seps = set(llm_seps) & set(exp_seps)
        max_seps = set(llm_seps) | set(exp_seps)
        struct_score = len(common_seps) / len(max_seps) if max_seps else 0.0
    else:
        struct_score = 0.5  # 无结构信息时中性

    # 加权综合
    similarity = 0.40 * num_score + 0.30 * keyword_score + 0.30 * struct_score
    similarity = max(0.0, min(1.0, similarity))

    logger.debug(
        f"[COMPARE] num={num_score:.3f} kw={keyword_score:.3f} struct={struct_score:.3f} → {similarity:.3f}"
    )
    return similarity


# ════════════════════════════════════════════════
# 轻量级激活记录（不重算 confidence，仅记录）
# ════════════════════════════════════════════════

def record_activation_only(
    exp_id: str,
    decision: str,
    triggered_by: str = "",
    output_summary: str = "",
) -> None:
    """
    轻量级激活记录: 只更新 frequency/last_used 并写 activations 表，
    不更新 success/failure 计数也不重算 confidence。

    用于 delegate 路径中经验存在但 LLM 完整输出后不确定是否匹配的场景。
    """
    from core.experience_store import exp_get, _get_conn

    exp = exp_get(exp_id)
    if not exp:
        return

    now = datetime.now().isoformat()
    freq = exp.get("frequency", 0) + 1
    total = exp.get("total_usage", 0) + 1

    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE exp_experiences SET frequency=?, total_usage=?, last_used=?, updated_at=? WHERE exp_id=?",
            (freq, total, now, now, exp_id),
        )
        conn.execute("""
            INSERT INTO exp_activations
            (exp_id, triggered_by, score_breakdown, confidence_before, confidence_after,
             result_success, output_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            exp_id, triggered_by,
            json.dumps({"decision": decision, "type": "activation_only"}, ensure_ascii=False),
            exp.get("confidence", 0), exp.get("confidence", 0),
            -1,  # neutral
            output_summary[:2000],
            now,
        ))
        conn.commit()
    finally:
        conn.close()


# ════════════════════════════════════════════════
# 内部工具函数
# ════════════════════════════════════════════════

def _auto_detect_success(exp_output: Optional[Dict], exp: Dict) -> Optional[bool]:
    """
    自动检测经验执行是否成功。
    基于输出质量和模板填充率判断。
    """
    if not exp_output or not isinstance(exp_output, dict):
        return False

    # ── 回放模式：习惯回放 = 成功执行 ──
    if exp_output.get("_replay_mode"):
        return True

    # 检查 code_executed 模式的输出
    if exp_output.get("_code_executed"):
        reply = exp_output.get("reply", "")
        if reply and len(reply) > 20:
            return True
        if not reply:
            return False

    # 模板模式: 检测残留占位符
    context_vars = exp.get("context_variables", [])
    if not context_vars:
        # 无上下文变量，检查是否有实质内容
        for k, v in exp_output.items():
            if not k.startswith("_") and isinstance(v, str) and v.strip():
                return True
        return False

    template_pattern = re.compile(r'\{(\w+)')
    filled_count = 0
    unfilled_count = 0

    for key, val in exp_output.items():
        if not isinstance(val, str) or key.startswith("_"):
            continue
        residual = template_pattern.findall(val)
        has_residual = any(v in context_vars for v in residual)
        if has_residual:
            unfilled_count += 1
        elif val.strip() and val != '(空)':
            filled_count += 1

    total = filled_count + unfilled_count
    if total == 0:
        return None

    fill_rate = filled_count / total
    if fill_rate >= 0.5:
        return True
    elif fill_rate < 0.2:
        return False
    return None


def _extract_exp_text(exp_output: Optional[Dict]) -> str:
    """从经验输出中提取文本内容用于对比"""
    if not exp_output:
        return ""
    parts = []
    # 优先 reply
    reply = exp_output.get("reply", "")
    if reply:
        parts.append(reply)
    # 模板字段
    for k, v in exp_output.items():
        if k.startswith("_") or k == "reply":
            continue
        if isinstance(v, str) and v.strip():
            parts.append(v)
        elif isinstance(v, (list, dict)):
            parts.append(json.dumps(v, ensure_ascii=False)[:200])
    return "\n".join(parts)


def _clean_text(text: str) -> str:
    """清理文本: 移除 markdown 标记、多余空格"""
    if not text:
        return ""
    # 移除 markdown 标记
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'[#*`_~|]', ' ', text)
    # 移除 emoji
    text = re.sub(r'[\U0001F000-\U0001FFFF]', ' ', text)
    # 标准化空格
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _extract_numbers(text: str) -> List[float]:
    """从文本中提取数值（金额、百分比等）"""
    if not text:
        return []
    # 匹配 ¥/￥ 后的数字、纯数字+万/千/亿、百分比
    patterns = [
        r'[¥￥]\s*([\d,.]+)',
        r'([\d,.]+)\s*万',
        r'([\d,.]+)\s*千',
        r'([\d,.]+)\s*亿',
        r'([\d,.]+)\s*%',
        r'([\d,.]+)\s*元',
    ]
    numbers = []
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            try:
                val = float(m.group(1).replace(',', ''))
                numbers.append(val)
            except ValueError:
                pass
    # 也提取裸数字（3位以上，过滤年份等）
    for m in re.finditer(r'(?<!\d)(\d{3,}(?:\.\d+)?)', text):
        try:
            val = float(m.group(1))
            if val > 100:  # 过滤小数字
                numbers.append(val)
        except ValueError:
            pass
    return numbers


def _compare_numbers(llm_nums: List[float], exp_nums: List[float]) -> float:
    """对比两组数值的相似度"""
    if not llm_nums and not exp_nums:
        return 0.5  # 双方无数值，中性
    if not llm_nums or not exp_nums:
        return 0.0  # 一方有数值一方没有

    # 匹配: 对每个 LLM 数值，找经验中最接近的
    matched = 0
    for ln in llm_nums[:10]:  # 最多取10个
        for en in exp_nums:
            if en == 0:
                continue
            diff = abs(ln - en) / max(abs(ln), abs(en), 1)
            if diff < 0.05:  # 5% 以内视为匹配
                matched += 1
                break

    # 匹配率
    total = min(len(llm_nums), len(exp_nums), 10)
    if total == 0:
        return 0.0
    return matched / total


def _tokenize(text: str) -> List[str]:
    """简单分词: 中文按字、英文按词"""
    if not text:
        return []
    tokens = []
    # 英文单词
    for m in re.finditer(r'[a-zA-Z]{2,}', text):
        tokens.append(m.group(0).lower())
    # 中文词语（2-4字）
    for m in re.finditer(r'[\u4e00-\u9fa5]{2,4}', text):
        tokens.append(m.group(0))
    return tokens


def _extract_structure(text: str) -> List[str]:
    """提取文本的结构标记（如表格、列表、段落分隔等）"""
    structures = []
    # 表格
    if '|' in text and '-' in text:
        structures.append("table")
    # 列表项
    if re.search(r'^\s*[-•·]\s', text, re.MULTILINE):
        structures.append("list")
    # 数字列表
    if re.search(r'^\s*\d+[.)]\s', text, re.MULTILINE):
        structures.append("numbered_list")
    # 标题
    if re.search(r'^#{1,6}\s', text, re.MULTILINE):
        structures.append("heading")
    # 进度条/百分比
    if '%' in text:
        structures.append("percentage")
    # 金额
    if '¥' in text or '￥' in text:
        structures.append("currency")
    # 段落数
    paragraphs = [p for p in text.split('\n') if p.strip()]
    if len(paragraphs) > 3:
        structures.append("multi_paragraph")
    return structures
