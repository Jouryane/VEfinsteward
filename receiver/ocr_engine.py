"""
VE4 OCR 引擎（精简版）
====================
一个函数完成 OCR：图像预处理 + Tesseract 识别。
无注册表、无装饰器、无评分系统。

依赖（模块化安装）：
    pip install pytesseract Pillow opencv-python numpy
    Tesseract-OCR 可执行文件 + chi_sim.traineddata
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime

logger = logging.getLogger("ve4.ocr")

# ── Tesseract 路径 ──
_TESS_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]
_TESS_USER_TESSDATA = Path(r"c:\Users\15284\.trae-cn\work\6a3577b608e054d7ee82f005\tessdata")
_configured = False


def ve4_ocr(file_path: Path) -> Optional[Dict]:
    """
    从图片中提取文字（预处理 + Tesseract）。

    Returns:
        {"raw_text": str, "engine": str, "preprocess": str} 或 None
    """
    global _configured
    if not _configured:
        for p in _TESS_PATHS:
            if Path(p).exists():
                if _TESS_USER_TESSDATA.exists() and (_TESS_USER_TESSDATA / "chi_sim.traineddata").exists():
                    os.environ["TESSDATA_PREFIX"] = str(_TESS_USER_TESSDATA)
                else:
                    os.environ["TESSDATA_PREFIX"] = str(Path(p).parent / "tessdata")
                _configured = True
                break

    try:
        import pytesseract
        from PIL import Image

        for p in _TESS_PATHS:
            if Path(p).exists():
                pytesseract.pytesseract.tesseract_cmd = p
                break

        # 图像预处理
        processed, method = _preprocess(file_path)

        text = pytesseract.image_to_string(processed, lang="chi_sim+eng")
        if text.strip():
            logger.info(f"[OCR] {method}: {len(text)} 字符 from {file_path.name}")
            return {
                "raw_text": text,
                "engine": "tesseract_enhanced",
                "preprocess": method,
                "extracted_at": datetime.now().isoformat(),
            }
        return None
    except ImportError as e:
        logger.error(f"[OCR] 依赖缺失: {e}")
        return None
    except Exception as e:
        logger.error(f"[OCR] 失败: {e}")
        return None


def _preprocess(file_path: Path):
    """图像预处理：灰度→放大→CLAHE→去噪→二值化。失败则返回原图。"""
    try:
        import cv2
        import numpy as np
        from PIL import Image

        img = Image.open(file_path)
        gray = img.convert("L")
        arr = np.array(gray)

        # 2x 放大
        h, w = arr.shape[:2]
        arr = cv2.resize(arr, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)

        # CLAHE 对比度增强
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        arr = clahe.apply(arr)

        # 中值去噪
        arr = cv2.medianBlur(arr, 3)

        # Otsu 二值化
        _, arr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        return Image.fromarray(arr), "opencv_full"
    except ImportError:
        pass

    # PIL 轻量降级
    try:
        from PIL import Image, ImageEnhance, ImageFilter
        img = Image.open(file_path).convert("L")
        w, h = img.size
        img = img.resize((w * 2, h * 2), Image.LANCZOS)
        img = ImageEnhance.Contrast(img).enhance(2.0)
        img = img.filter(ImageFilter.SHARPEN)
        return img, "pil_light"
    except Exception:
        pass

    # 无预处理
    try:
        return Image.open(file_path), "none"
    except Exception:
        raise RuntimeError(f"无法读取图片: {file_path}")
