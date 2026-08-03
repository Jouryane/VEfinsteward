"""
VE5 Experience Store — 重导出包装器
======================================
实际实现在 core.experience_store.py。
此文件仅为兼容性保留，所有调用都会委托到 experience_store。
"""

# ════════════════════════════════════════════════
# 从主 store 模块导入全部公开 API
# ════════════════════════════════════════════════

from core.experience_store import (
    # 常量
    _LAMBDA_DECAY,
    _AUTOMATIC_THRESHOLD,
    _LEARNING_THRESHOLD,
    _FREQUENCY_CAP,
    _SIMILARITY_THRESHOLD,

    # 初始化
    exp_init,

    # Confidence
    _compute_confidence_v1,
    _estimate_data_stability,
    _level_from_confidence,
    _get_level,

    # CRUD
    exp_create,
    exp_get,
    exp_list,
    exp_delete,
    exp_update,

    # 核心
    exp_hit,
    exp_execute,
    exp_decay_tick,

    # 自动反馈
    _auto_detect_feedback,

    # 模板
    _fill_template,
)
