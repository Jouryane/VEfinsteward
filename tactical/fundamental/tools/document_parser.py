"""
VE4 文档解析工具 (DocumentParser)
==================================
解析研报和新闻文档：
    - URL 抓取（网页内容提取）
    - PDF 解析（文本提取）

命名规范：
    - 类名: VE4DocumentParser
    - 函数名: ve4_tactical_parse_{action}
"""

import re
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger("ve4.tactical.document_parser")


class VE4DocumentParser:
    """文档解析工具 —— URL 抓取 + PDF 文本提取"""

    def __init__(self):
        pass

    async def parse(self, source: str) -> Dict[str, Any]:
        """
        解析文档来源（URL 或 PDF 路径）。

        Args:
            source: URL 或本地 PDF 文件路径

        Returns:
            {"success": bool, "title": str, "text": str, "source": str, "error": str}
        """
        if source.startswith("http://") or source.startswith("https://"):
            return await self._parse_url(source)
        elif source.lower().endswith(".pdf"):
            return await self._parse_pdf(source)
        else:
            return {"success": False, "title": "", "text": "", "source": source,
                    "error": "不支持的来源格式（请提供 URL 或 PDF 路径）"}

    async def _parse_url(self, url: str) -> Dict[str, Any]:
        """抓取 URL 并提取正文"""
        try:
            import urllib.request
            from urllib.error import URLError

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.0",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }
            )

            with urllib.request.urlopen(req, timeout=20) as response:
                html = response.read().decode("utf-8", errors="ignore")

            # 智能提取：根据不同站点选择最佳提取策略
            title = self._extract_title_smart(html, url)
            text = self._extract_content_smart(html, url)

            if not text or len(text) < 50:
                # fallback 到简单 HTML 清理
                text = self._clean_html(html)

            return {
                "success": True,
                "title": title,
                "text": text[:20000],
                "source": url,
                "error": "",
            }

        except URLError as e:
            logger.error(f"[PARSER] URL 抓取失败: {url} - {e}")
            return {"success": False, "title": "", "text": "", "source": url, "error": f"网络请求失败: {e}"}
        except Exception as e:
            logger.error(f"[PARSER] URL 解析异常: {url} - {e}")
            return {"success": False, "title": "", "text": "", "source": url, "error": str(e)}

    async def _parse_pdf(self, pdf_path: str) -> Dict[str, Any]:
        """解析 PDF 文件提取文本"""
        path = Path(pdf_path)
        if not path.exists():
            return {"success": False, "title": "", "text": "", "source": pdf_path,
                    "error": f"文件不存在: {pdf_path}"}

        try:
            # 尝试 PyPDF2
            try:
                import PyPDF2
                return self._parse_pdf_pypdf2(path)
            except ImportError:
                pass

            # 回退：pdfplumber
            try:
                import pdfplumber
                return self._parse_pdf_pdfplumber(path)
            except ImportError:
                pass

            # 最终回退：报告缺少依赖
            return {"success": False, "title": "", "text": "", "source": pdf_path,
                    "error": "缺少 PDF 解析库（请安装 PyPDF2 或 pdfplumber）"}

        except Exception as e:
            logger.error(f"[PARSER] PDF 解析异常: {pdf_path} - {e}")
            return {"success": False, "title": "", "text": "", "source": pdf_path, "error": str(e)}

    def _parse_pdf_pypdf2(self, path: Path) -> Dict[str, Any]:
        """使用 PyPDF2 解析"""
        import PyPDF2
        text_parts = []
        with open(path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            title = reader.metadata.title if reader.metadata else path.stem
            for page in reader.pages:
                text_parts.append(page.extract_text() or "")

        full_text = "\n".join(text_parts)
        return {
            "success": True,
            "title": title or path.stem,
            "text": full_text[:20000],
            "source": str(path),
            "error": "",
        }

    def _parse_pdf_pdfplumber(self, path: Path) -> Dict[str, Any]:
        """使用 pdfplumber 解析"""
        import pdfplumber
        text_parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")

        full_text = "\n".join(text_parts)
        return {
            "success": True,
            "title": path.stem,
            "text": full_text[:20000],
            "source": str(path),
            "error": "",
        }

    def _clean_html(self, html: str) -> str:
        """简单 HTML 清理"""
        # 移除 script 和 style
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
        # 移除标签
        html = re.sub(r"<[^>]+>", " ", html)
        # 合并空白
        html = re.sub(r"\s+", " ", html)
        return html.strip()

    def _extract_title(self, html: str) -> str:
        """从 HTML 提取标题"""
        match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
        match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
        if match:
            return re.sub(r"<[^>]+>", "", match.group(1)).strip()
        return ""

    def _extract_title_smart(self, html: str, url: str) -> str:
        """智能标题提取（支持微信公众号等平台）"""
        # 1. 标准 <title>
        title = self._extract_title(html)
        if title and len(title) > 2:
            return title

        # 2. 微信公众号：var msg_title = "..."
        m = re.search(r'var\s+msg_title\s*=\s*["\'](.*?)["\']', html)
        if m:
            return m.group(1).strip()

        # 3. OG meta
        m = re.search(r'<meta[^>]*property=["\']og:title["\'][^>]*content=["\'](.*?)["\']', html, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m = re.search(r'<meta[^>]*content=["\'](.*?)["\'][^>]*property=["\']og:title["\']', html, re.IGNORECASE)
        if m:
            return m.group(1).strip()

        # 4. <h1>
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
        if m:
            return re.sub(r"<[^>]+>", "", m.group(1)).strip()

        # 5. 兜底域名
        return urlparse(url).netloc

    def _extract_content_smart(self, html: str, url: str) -> str:
        """智能正文提取（支持微信公众号等平台）"""
        host = urlparse(url).netloc.lower()

        # 微信公众号文章
        if "mp.weixin.qq.com" in host or "weixin" in host:
            return self._extract_wechat_article(html)

        # 东方财富 / 同花顺等财经网站
        if any(kw in host for kw in ["eastmoney", "10jqka", "finance.sina", "finance.qq"]):
            return self._extract_finance_article(html)

        # 通用 fallback
        return self._clean_html(html)

    def _extract_wechat_article(self, html: str) -> str:
        """提取微信公众号文章正文"""
        # 微信文章正文在 id="js_content" 的 <div> 中
        m = re.search(r'<div[^>]*id=["\']js_content["\'][^>]*>(.*?)</div>\s*<script', html, re.DOTALL)
        if not m:
            m = re.search(r'<div[^>]*id=["\']js_content["\'][^>]*>(.*?)</div>', html, re.DOTALL)
        if not m:
            # 尝试 rich_media_content
            m = re.search(r'class=["\']rich_media_content["\'][^>]*>(.*?)</div>\s*<script', html, re.DOTALL)

        if m:
            content_html = m.group(1)
            return self._clean_html(content_html)

        # fallback: 全文清理
        return self._clean_html(html)

    def _extract_finance_article(self, html: str) -> str:
        """提取财经网站文章正文"""
        # 尝试常见正文容器
        for container_id in ["ContentBody", "txtcontent", "Content", "article-content", "main-content"]:
            m = re.search(
                rf'<div[^>]*id=["\']{container_id}["\'][^>]*>(.*?)</div>',
                html, re.DOTALL
            )
            if m and len(m.group(1)) > 200:
                return self._clean_html(m.group(1))

        # 尝试 <article> 标签
        m = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL)
        if m:
            return self._clean_html(m.group(1))

        return self._clean_html(html)
