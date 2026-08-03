"""
VE4 OCR 引擎模块（双轨并行版）
================================
可插拔的 OCR 后端，支持用户自由选择文字提取方式。

可用后端：
    - tesseract         : 本地 Tesseract OCR（原始图片，完全离线）
    - tesseract_enhanced: 本地 Tesseract OCR + 图像预处理（灰度化/二值化/放大）
    - easyocr           : 本地 EasyOCR（完全离线，备选）
    - llm_vision        : LLM 视觉模型（通过 ai_gateway，统一AI配置，本地优先）

双轨并行模式（dual_track）：
    同时执行 tesseract_enhanced + llm_vision，取最优结果。
    适用于彩色UI截图（如东方财富App），单引擎OCR质量不足时自动补充。

配置方式：
    在 ve4_settings.json 中设置：
    {
        "ocr_mode": "dual_track"   // dual_track | tesseract_only | llm_only | auto
    }

依赖说明（模块化安装）：
    - tesseract / tesseract_enhanced : 需要 pytesseract + Pillow + Tesseract-OCR 可执行文件
    - easyocr                        : 需要 easyocr（独立安装）
    - llm_vision                     : 需要 ai_gateway + 用户配置的 AI API（本地Ollama或云端）
    图像预处理函数与 Tesseract 后端放在同一文件中，用户选择安装 Tesseract 时即获得预处理能力。
"""

import os
import json
import logging
import base64
from pathlib import Path
from typing import Dict, List, Optional, Any
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger("ve4.ocr")

# ── Tesseract 路径自动检测 ──
_POSSIBLE_TESSERACT_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]
_USER_TESSDATA = Path(r"c:\Users\15284\.trae-cn\work\6a3577b608e054d7ee82f005\tessdata")
_TESSERACT_CONFIGURED = False

def ve4_ocr_configure_tesseract():
    """自动配置 Tesseract 路径和语言包环境变量"""
    global _TESSERACT_CONFIGURED
    if _TESSERACT_CONFIGURED:
        return
    for tess_path in _POSSIBLE_TESSERACT_PATHS:
        if Path(tess_path).exists():
            # 优先使用用户级 tessdata（含中文语言包）
            if _USER_TESSDATA.exists() and (_USER_TESSDATA / "chi_sim.traineddata").exists():
                os.environ["TESSDATA_PREFIX"] = str(_USER_TESSDATA)
            else:
                os.environ["TESSDATA_PREFIX"] = str(Path(tess_path).parent / "tessdata")
            _TESSERACT_CONFIGURED = True
            return


# ══════════════════════════════════════════════════════════
# 图像预处理（与 Tesseract 同文件，确保安装 Tesseract 时即获得预处理能力）
# ══════════════════════════════════════════════════════════

def ve4_ocr_preprocess_image(file_path: Path) -> Optional[Any]:
    """
    图像预处理：针对彩色UI截图优化 Tesseract 识别率。

    处理步骤：
        1. 灰度化（去除彩色干扰）
        2. 对比度增强（CLAHE 自适应直方图均衡化）
        3. 自适应二值化（Otsu 阈值，将文字转为纯黑白）
        4. 2x 放大（提升小字号文字识别率）
        5. 中值滤波去噪（去除压缩伪影）

    Args:
        file_path: 图片文件路径

    Returns:
        PIL.Image 预处理后的图像对象，失败返回 None
    """
    try:
        from PIL import Image, ImageFilter
        import numpy as np
        import cv2

        # 读取图片
        image = Image.open(file_path)

        # Step 1: 灰度化
        if image.mode != 'L':
            gray = image.convert('L')
        else:
            gray = image

        # 转为 numpy 数组供 OpenCV 处理
        img_array = np.array(gray)

        # Step 2: 2x 放大（先放大再处理，保留更多细节）
        h, w = img_array.shape[:2]
        img_array = cv2.resize(img_array, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)

        # Step 3: CLAHE 对比度增强（自适应直方图均衡化）
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        img_array = clahe.apply(img_array)

        # Step 4: 中值滤波去噪（去除JPEG压缩伪影，保留文字边缘）
        img_array = cv2.medianBlur(img_array, 3)

        # Step 5: Otsu 自适应二值化（将文字转为纯黑白）
        _, img_array = cv2.threshold(
            img_array, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )

        # 转回 PIL Image
        processed = Image.fromarray(img_array)
        logger.info(f"[OCR-PREPROCESS] 预处理完成: {image.size} → {processed.size} (2x放大+灰度+CLAHE+Otsu)")
        return processed

    except ImportError:
        logger.warning("[OCR-PREPROCESS] cv2/numpy 未安装，跳过预处理（pip install opencv-python numpy）")
        return None
    except Exception as e:
        logger.warning(f"[OCR-PREPROCESS] 预处理失败: {e}")
        return None


def ve4_ocr_preprocess_light(file_path: Path) -> Optional[Any]:
    """
    轻量级图像预处理（不依赖 OpenCV，仅用 PIL）。

    适用于未安装 opencv-python 的环境。
    处理步骤：灰度化 → 对比度增强 → 2x 放大 → 锐化。
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter

        image = Image.open(file_path)

        # 灰度化
        if image.mode != 'L':
            gray = image.convert('L')
        else:
            gray = image

        # 2x 放大
        w, h = gray.size
        gray = gray.resize((w * 2, h * 2), Image.LANCZOS)

        # 对比度增强
        enhancer = ImageEnhance.Contrast(gray)
        gray = enhancer.enhance(2.0)

        # 锐化
        gray = gray.filter(ImageFilter.SHARPEN)

        logger.info(f"[OCR-PREPROCESS-LIGHT] 轻量预处理完成: {image.size} → {gray.size}")
        return gray

    except ImportError:
        logger.warning("[OCR-PREPROCESS-LIGHT] Pillow 未安装")
        return None
    except Exception as e:
        logger.warning(f"[OCR-PREPROCESS-LIGHT] 预处理失败: {e}")
        return None


# ── 后端注册表 ──
_OCR_BACKENDS: List[Dict[str, Any]] = []


def ve4_ocr_register(name: str, priority: int = 0, enabled: bool = True):
    """装饰器：注册一个 OCR 后端"""
    def decorator(func):
        _OCR_BACKENDS.append({
            "name": name,
            "priority": priority,
            "enabled": enabled,
            "func": func,
        })
        _OCR_BACKENDS.sort(key=lambda x: x["priority"])
        return func
    return decorator


# ══════════════════════════════════════════════════════════
# 主提取函数（支持双轨并行模式）
# ══════════════════════════════════════════════════════════

def ve4_ocr_extract_text(
    file_path: Path,
    preferred_backend: str = None,
    dual_track: bool = False,
    allow_cloud_for_privacy: bool = False,
) -> Optional[Dict]:
    """
    使用 OCR 从图片中提取文字。

    Args:
        file_path: 图片文件路径
        preferred_backend: 指定后端名称（可选）
        dual_track: 是否启用双轨并行模式（tesseract_enhanced + llm_vision 同时执行）
        allow_cloud_for_privacy: 是否允许云端模型处理含隐私数据的截图
            （默认 False，本地LLM优先不回退云端；用户可手动解除限制）

    Returns:
        dict: {"source_type": str, "raw_text": str, "ocr_engine": str, ...} 或 None
    """
    ve4_ocr_configure_tesseract()
    logger.info(f"[OCR] 开始提取：{file_path.name}（双轨: {'开启' if dual_track else '关闭'}）")

    # ── 双轨并行模式 ──
    if dual_track:
        return _extract_dual_track(file_path, allow_cloud_for_privacy)

    # ── 单轨模式：指定后端 ──
    if preferred_backend:
        for backend in _OCR_BACKENDS:
            if backend["name"] == preferred_backend and backend["enabled"]:
                result = backend["func"](file_path)
                if result and result.get("raw_text", "").strip():
                    logger.info(f"[OCR] {preferred_backend} 成功，提取 {len(result['raw_text'])} 字符")
                    return result
                else:
                    logger.warning(f"[OCR] {preferred_backend} 未提取到文字")
        logger.error(f"[OCR] 指定后端 {preferred_backend} 失败")
        return None

    # ── 单轨模式：自动按优先级尝试 ──
    for backend in _OCR_BACKENDS:
        if not backend["enabled"]:
            continue
        result = backend["func"](file_path)
        if result and result.get("raw_text", "").strip():
            logger.info(f"[OCR] {backend['name']} 成功，提取 {len(result['raw_text'])} 字符")
            return result
        else:
            logger.info(f"[OCR] {backend['name']} 未提取到文字，尝试下一个")

    logger.error(f"[OCR] 所有引擎均失败：{file_path.name}")
    return None


def _extract_dual_track(file_path: Path, allow_cloud: bool = False) -> Optional[Dict]:
    """
    双轨并行：同时执行 Tesseract(增强) 和 LLM视觉，取最优结果。

    Track A: tesseract_enhanced（本地，快速，免费）
    Track B: llm_vision（通过ai_gateway，本地优先，隐私数据不回退云端）

    选择策略：
        - 如果两轨都成功：比较中文+数字字符数，取信息量更大者
        - 如果只有一轨成功：取成功者
        - 如果都失败：返回 None
    """
    logger.info(f"[OCR-DUAL] 双轨并行启动：{file_path.name}")

    results = {}

    with ThreadPoolExecutor(max_workers=2) as executor:
        # Track A: Tesseract Enhanced
        future_a = executor.submit(_ocr_tesseract_enhanced, file_path)
        # Track B: LLM Vision（含隐私数据，本地优先）
        future_b = executor.submit(_ocr_llm_vision, file_path, True, allow_cloud)

        for future in as_completed([future_a, future_b]):
            try:
                result = future.result(timeout=60)
                if result and result.get("raw_text", "").strip():
                    engine = result.get("ocr_engine", "unknown")
                    results[engine] = result
                    logger.info(f"[OCR-DUAL] {engine} 完成: {len(result['raw_text'])} 字符")
                else:
                    logger.info(f"[OCR-DUAL] 某轨未提取到文字")
            except Exception as e:
                logger.warning(f"[OCR-DUAL] 某轨异常: {e}")

    if not results:
        logger.error(f"[OCR-DUAL] 双轨均失败：{file_path.name}")
        return None

    if len(results) == 1:
        # 只有一轨成功
        winner = list(results.values())[0]
        winner["dual_track_mode"] = "single_success"
        return winner

    # 两轨都成功：比较信息量，取更优
    best_engine = None
    best_score = -1
    for engine, result in results.items():
        score = _score_ocr_result(result["raw_text"])
        logger.info(f"[OCR-DUAL] {engine} 评分: {score:.1f}")
        if score > best_score:
            best_score = score
            best_engine = engine

    winner = results[best_engine]
    winner["dual_track_mode"] = "best_of_two"
    winner["dual_track_all_engines"] = list(results.keys())
    winner["dual_track_all_scores"] = {e: _score_ocr_result(r["raw_text"]) for e, r in results.items()}
    logger.info(f"[OCR-DUAL] 选定最优: {best_engine} (评分 {best_score:.1f})")
    return winner


def _score_ocr_result(text: str) -> float:
    """
    评估 OCR 结果质量（信息量越大分越高）。

    评分维度：
        - 中文字符数（权重 1.0）
        - 数字字符数（权重 1.2，金融截图数字更重要）
        - 有效行数（权重 0.5，结构完整性）
        - 乱码惩罚（含特殊符号扣分）
    """
    import re
    # 中文字符
    cn_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    # 数字和金额
    digits = len(re.findall(r'[0-9]', text))
    # 有效行（非空非纯空白）
    lines = [l for l in text.split('\n') if l.strip()]
    line_count = len(lines)
    # 乱码符号惩罚
    garbage = len(re.findall(r'[<>¢©®§¶•·»«~]', text))

    score = cn_chars * 1.0 + digits * 1.2 + line_count * 0.5 - garbage * 2.0
    return max(0, score)


# ══════════════════════════════════════════════════════════
# 内置后端实现
# ══════════════════════════════════════════════════════════

@ve4_ocr_register("tesseract", priority=1)
def _ocr_tesseract(file_path: Path) -> Optional[Dict]:
    """本地 Tesseract OCR（原始图片，完全离线）"""
    try:
        import pytesseract
        from PIL import Image
        for tess_path in _POSSIBLE_TESSERACT_PATHS:
            if Path(tess_path).exists():
                pytesseract.pytesseract.tesseract_cmd = tess_path
                break
        image = Image.open(file_path)
        text = pytesseract.image_to_string(image, lang='chi_sim+eng')
        if text.strip():
            return {
                "source_type": "image_ocr_tesseract",
                "file_path": str(file_path),
                "raw_text": text,
                "image_size": image.size,
                "ocr_engine": "local_tesseract",
                "extracted_at": datetime.now().isoformat(),
            }
    except ImportError:
        logger.warning("[OCR] pytesseract 未安装：pip install pytesseract")
    except Exception as e:
        logger.warning(f"[OCR] tesseract 失败：{e}")
    return None


@ve4_ocr_register("tesseract_enhanced", priority=1)
def _ocr_tesseract_enhanced(file_path: Path) -> Optional[Dict]:
    """
    本地 Tesseract OCR + 图像预处理（针对彩色UI截图优化）。

    预处理流程：灰度化 → CLAHE对比度增强 → 2x放大 → 中值去噪 → Otsu二值化
    如果 OpenCV 不可用，回退到轻量级 PIL 预处理。
    如果预处理完全失败，回退到原始 Tesseract。
    """
    try:
        import pytesseract
        from PIL import Image

        for tess_path in _POSSIBLE_TESSERACT_PATHS:
            if Path(tess_path).exists():
                pytesseract.pytesseract.tesseract_cmd = tess_path
                break

        # 尝试完整预处理（OpenCV）
        processed = ve4_ocr_preprocess_image(file_path)
        preprocess_method = "opencv_full"

        # 回退到轻量预处理（PIL only）
        if processed is None:
            processed = ve4_ocr_preprocess_light(file_path)
            preprocess_method = "pil_light"

        # 回退到原始图片
        if processed is None:
            processed = Image.open(file_path)
            preprocess_method = "none"

        text = pytesseract.image_to_string(processed, lang='chi_sim+eng')
        if text.strip():
            return {
                "source_type": "image_ocr_tesseract_enhanced",
                "file_path": str(file_path),
                "raw_text": text,
                "image_size": processed.size,
                "ocr_engine": "local_tesseract_enhanced",
                "preprocess_method": preprocess_method,
                "extracted_at": datetime.now().isoformat(),
            }
    except ImportError:
        logger.warning("[OCR] pytesseract 未安装：pip install pytesseract")
    except Exception as e:
        logger.warning(f"[OCR] tesseract_enhanced 失败：{e}")
    return None


@ve4_ocr_register("easyocr", priority=2)
def _ocr_easyocr(file_path: Path) -> Optional[Dict]:
    """本地 EasyOCR（备选，完全离线）"""
    try:
        import easyocr
        reader = easyocr.Reader(['ch_sim', 'en'], verbose=False)
        result = reader.readtext(str(file_path))
        text = "\n".join([r[1] for r in result])
        if text.strip():
            return {
                "source_type": "image_ocr_easyocr",
                "file_path": str(file_path),
                "raw_text": text,
                "ocr_engine": "local_easyocr",
                "extracted_at": datetime.now().isoformat(),
            }
    except ImportError:
        logger.warning("[OCR] easyocr 未安装：pip install easyocr")
    except Exception as e:
        logger.warning(f"[OCR] easyocr 失败：{e}")
    return None


@ve4_ocr_register("llm_vision", priority=5)
def _ocr_llm_vision(
    file_path: Path,
    contains_privacy_data: bool = True,
    allow_cloud_fallback: bool = False,
) -> Optional[Dict]:
    """
    LLM 视觉模型 OCR（通过 ai_gateway 统一路由，本地优先）。

    隐私规则：
        - 含隐私数据（持仓截图）→ 本地 LLM 优先，默认不回退云端
        - 用户可手动解除限制（allow_cloud_fallback=True）
        - 不含隐私数据 → 云端优先，回退本地

    依赖：
        - 本地: Ollama + llava:7b（或其它视觉模型）
        - 云端: OpenAI兼容API + gpt-4o-mini（或其它视觉模型）
        AI 配置统一从 ai_providers.yaml + ai_settings 表读取。
    """
    try:
        from core.ai_gateway import ve4_ai_extract_text_from_image

        # 财务截图含隐私数据（持仓名称、收益），默认本地优先不回退云端
        # 用户可通过 allow_cloud_fallback 手动解除限制
        effective_privacy = contains_privacy_data and not allow_cloud_fallback

        vision_result = ve4_ai_extract_text_from_image(
            image_path=str(file_path),
            contains_privacy_data=effective_privacy,
        )

        if vision_result.success and vision_result.text.strip():
            return {
                "source_type": "image_vision_llm",
                "file_path": str(file_path),
                "raw_text": vision_result.text,
                "ocr_engine": f"llm_{vision_result.provider}",
                "vision_confidence": getattr(vision_result, 'confidence', 0),
                "contains_privacy": effective_privacy,
                "extracted_at": datetime.now().isoformat(),
            }
        else:
            error_msg = getattr(vision_result, 'error', '未知错误')
            logger.warning(f"[OCR] LLM视觉失败：{error_msg}")
            return None
    except ImportError:
        logger.warning("[OCR] ai_gateway 不可用（core/ai_gateway.py 未找到）")
    except Exception as e:
        logger.warning(f"[OCR] LLM视觉异常：{e}")
    return None


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════

def ve4_ocr_list_backends() -> List[Dict]:
    """返回所有注册的后端及其状态"""
    ve4_ocr_configure_tesseract()
    return [
        {
            "name": b["name"],
            "priority": b["priority"],
            "enabled": b["enabled"],
        }
        for b in _OCR_BACKENDS
    ]


def ve4_ocr_set_backend(name: str, enabled: bool):
    """启用/禁用指定后端"""
    for b in _OCR_BACKENDS:
        if b["name"] == name:
            b["enabled"] = enabled
            return True
    return False


def ve4_ocr_get_settings() -> Dict:
    """获取当前 OCR 配置"""
    return {
        "backends": ve4_ocr_list_backends(),
        "dual_track_available": True,
        "preprocess_available": _check_preprocess_deps(),
    }


def _check_preprocess_deps() -> Dict:
    """检查图像预处理依赖是否可用"""
    deps = {"pil": False, "numpy": False, "cv2": False}
    try:
        from PIL import Image
        deps["pil"] = True
    except ImportError:
        pass
    try:
        import numpy
        deps["numpy"] = True
    except ImportError:
        pass
    try:
        import cv2
        deps["cv2"] = True
    except ImportError:
        pass
    return deps
