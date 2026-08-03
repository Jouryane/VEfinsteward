"""
VE5 统一 AI 调用网关
====================
所有 AI 调用的唯一入口。其他模块禁止直接调用模型。

核心职责：
    1. 从 ai_providers.yaml 读取配置
    2. 根据路由规则选择 provider（本地/云端）
    3. 执行调用（含超时、重试、降级）
    4. 记录调用日志（provider、耗时、token数）
    5. 返回结构化结果（含推理过程，确保可追溯）

使用规范：
    - 模块外部只能通过 ve4_ai_call() 使用
    - 禁止在 ai_gateway.py 之外的地方直接调用模型 API
    - 所有函数前缀：ve4_ai_
    - task_type 必须在 ai_providers.yaml 的 use_for 中注册
"""

import os
import json
import time
import logging
import hashlib
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, Any, List

logger = logging.getLogger("ve5.ai_gateway")

# ─── 配置文件路径 ───
from app_paths import DB_PATH, CONFIG_DIR

_GATEWAY_DIR = Path(__file__).parent.resolve()
_CONFIG_PATH = CONFIG_DIR / "ai_providers.yaml"

# ─── 常量 ───
_OLLAMA_CHAT_URL_TEMPLATE = "{base_url}/v1/chat/completions"
_OPENAI_CHAT_URL_TEMPLATE = "{base_url}/chat/completions"
_DEFAULT_TIMEOUT = 60
_DEFAULT_MAX_TOKENS = 256
_DEFAULT_TEMPERATURE = 0.1

# ── 推理模型自动检测 ──
_REASONING_MODEL_KEYWORDS = [
    "deepseek-reasoner", "deepseek-r1", "deepseek-v4",
    "o1", "o3", "o4",
    "qwq", "qwen-qwq",
    "reasoner", "thinking",
]


def _detect_reasoning_model(model_name: str) -> bool:
    """根据模型名自动检测是否为推理模型"""
    name_lower = model_name.lower()
    for kw in _REASONING_MODEL_KEYWORDS:
        if kw in name_lower:
            return True
    return False


# ════════════════════════════════════════════════════════════════
# 数据模型
# ════════════════════════════════════════════════════════════════

@dataclass
class VE4AiProvider:
    """AI 提供商配置"""
    name: str
    type: str  # "ollama" | "openai_compatible"
    base_url: str
    model: str
    api_key: str = ""
    timeout: int = _DEFAULT_TIMEOUT
    max_tokens: int = _DEFAULT_MAX_TOKENS
    temperature: float = _DEFAULT_TEMPERATURE
    priority: int = 99
    cache_enabled: bool = True
    use_for: List[str] = field(default_factory=list)
    is_reasoning_model: bool = False       # 推理模型标记（DeepSeek-R1, QwQ, o1等）
    reasoning_reserve: int = 0             # 推理预留 token（实际 max_tokens = max_tokens + reserve）


@dataclass
class VE4AiRequest:
    """AI 调用请求"""
    task_type: str  # 任务类型，用于路由
    system: str = ""
    prompt: str = ""
    max_tokens: int = 0  # 0 表示使用 provider 默认值
    temperature: float = 0.0  # 0.0 表示使用 provider 默认值
    format_type: str = "text"  # "text" | "json"
    contains_privacy_data: bool = False
    complexity: str = "low"  # "low" | "medium" | "high"
    force_provider: str = ""  # 强制使用指定 provider，空=自动路由


@dataclass
class VE4AiResult:
    """AI 调用结果"""
    success: bool
    text: str = ""           # 模型正式输出（给用户看的内容）
    provider: str = ""
    model: str = ""
    confidence: float = 1.0
    duration_ms: int = 0
    cached: bool = False
    error: str = ""
    explanation: str = ""    # 调用元信息（tokens、模型等）
    reasoning: str = ""      # 模型思考过程（reasoning_content），可选展示
    is_truncated: bool = False  # 输出被截断标记（推理模型 token 耗尽时常见）

    def to_dict(self) -> dict:
        return asdict(self)


# ════════════════════════════════════════════════════════════════
# 配置加载
# ════════════════════════════════════════════════════════════════

class _VE4AiConfigLoader:
    """AI 提供商配置加载器（内部单例）"""

    def __init__(self):
        self._providers: Dict[str, VE4AiProvider] = {}
        self._rules: List[Dict] = []
        self._defaults: Dict = {}
        self._loaded = False

    def load(self, force: bool = False):
        """加载 YAML 配置（缓存，除非强制刷新）"""
        if self._loaded and not force:
            return
        if not _CONFIG_PATH.exists():
            logger.warning(f"[AI-CONFIG] 配置文件不存在：{_CONFIG_PATH}，使用默认配置")
            self._load_defaults()
            return

        try:
            import yaml
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)

            # ── 解析 providers ──
            self._providers = {}
            for name, cfg in raw.get("providers", {}).items():
                api_key = cfg.get("api_key", "")
                if api_key.startswith("${"):
                    env_key = api_key[2:-1]  # ${TRAEV4_CLOUD_API_KEY} → TRAEV4_CLOUD_API_KEY
                    api_key = os.environ.get(env_key, "")
                    if not api_key:
                        logger.debug(f"[AI-CONFIG] 环境变量 {env_key} 未设置，{name} 将不可用")

                self._providers[name] = VE4AiProvider(
                    name=name,
                    type=cfg.get("type", "openai_compatible"),
                    base_url=cfg.get("base_url", ""),
                    model=cfg.get("model", ""),
                    api_key=api_key,
                    timeout=cfg.get("timeout", _DEFAULT_TIMEOUT),
                    max_tokens=cfg.get("max_tokens", _DEFAULT_MAX_TOKENS),
                    temperature=cfg.get("temperature", _DEFAULT_TEMPERATURE),
                    priority=cfg.get("priority", 99),
                    cache_enabled=cfg.get("cache_enabled", True),
                    use_for=cfg.get("use_for", []),
                    is_reasoning_model=cfg.get("is_reasoning_model", False) or _detect_reasoning_model(cfg.get("model", "")),
                    reasoning_reserve=cfg.get("reasoning_reserve", 4096 if cfg.get("is_reasoning_model") else 0),
                )

            # ── 解析路由规则 ──
            self._rules = raw.get("routing_rules", [])

            # ── 解析默认参数 ──
            self._defaults = raw.get("defaults", {})

            # ── 叠加用户配置的 AI 设置（数据库 ai_settings 表）──
            try:
                import sqlite3
                # 统一数据库路径：与 API server / pipeline 一致
                db_path = DB_PATH
                if db_path.exists():
                    conn = sqlite3.connect(str(db_path))
                    conn.row_factory = sqlite3.Row
                    # 确保表存在（与 API 层保持一致）
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS ai_settings (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            provider TEXT, api_key TEXT, api_base TEXT,
                            model TEXT, updated_at TEXT
                        )
                    """)
                    row = conn.execute("SELECT * FROM ai_settings WHERE id = 1").fetchone()
                    conn.close()
                    if row:
                        user = dict(row)
                        base_url = user.get("api_base", "").rstrip("/")
                        api_key = user.get("api_key", "")
                        model = user.get("model", "")
                        provider_type = "ollama" if "localhost" in base_url or ":11434" in base_url else "openai_compatible"
                        if base_url and api_key and model:
                            is_reasoning = _detect_reasoning_model(model)
                            self._providers["user_configured"] = VE4AiProvider(
                                name="user_configured",
                                type=provider_type,
                                base_url=base_url,
                                model=model,
                                api_key=api_key,
                                timeout=60 if provider_type == "openai_compatible" else 60,
                                max_tokens=1024,
                                temperature=0.3,
                                priority=0,  # 用户配置优先级最高
                                cache_enabled=False,
                                use_for=[],  # 所有任务都可用
                                is_reasoning_model=is_reasoning,
                                reasoning_reserve=4096 if is_reasoning else 0,
                            )
                            logger.info(f"[AI-CONFIG] 已加载用户配置 provider: {model} @ {base_url}"
                                        f"{' [推理模型]' if is_reasoning else ''}")
            except Exception as e:
                logger.debug(f"[AI-CONFIG] 读取用户配置失败（可能尚未配置）: {e}")

            self._loaded = True
            logger.info(f"[AI-CONFIG] 已加载 {len(self._providers)} 个提供商, "
                        f"{len(self._rules)} 条路由规则")

        except Exception as e:
            logger.error(f"[AI-CONFIG] 加载配置失败：{e}，使用默认配置")
            self._load_defaults()

    def _load_defaults(self):
        """无配置文件时的默认配置"""
        self._providers = {
            "local_alpha": VE4AiProvider(
                name="local_alpha",
                type="ollama",
                base_url="http://localhost:11434",
                model="qwen2:1.5b",
                timeout=10,
                max_tokens=256,
                temperature=0.1,
                priority=1,
                cache_enabled=True,
                use_for=["asset_classification", "liquidity_inference", "yes_no_question"],
            )
        }
        self._rules = [
            {"condition": "default", "provider": "local_alpha", "fallback": "none"}
        ]
        self._defaults = {"risk_free_rate": 2.5, "emergency_months_default": 3}
        self._loaded = True

    def get_provider(self, name: str) -> Optional[VE4AiProvider]:
        """获取指定 provider"""
        return self._providers.get(name)

    def get_providers_by_task(self, task_type: str) -> List[VE4AiProvider]:
        """获取支持指定任务类型的 provider，按优先级排序"""
        candidates = [p for p in self._providers.values() if not p.use_for or task_type in p.use_for]
        return sorted(candidates, key=lambda p: p.priority)

    def get_default(self, key: str, fallback=None):
        """获取默认参数值"""
        return self._defaults.get(key, fallback)

    def resolve_route(self, request: VE4AiRequest) -> tuple:
        """
        路由决策：根据请求和规则，返回 (primary_provider_name, fallback_provider_name)

        Returns:
            (primary, fallback) — fallback 为 "none" 表示不回退
        """
        for rule in self._rules:
            condition = rule["condition"]
            matched = False

            if condition == "contains_privacy_data":
                matched = request.contains_privacy_data
            elif condition == "contains_privacy_data and not complexity":
                matched = request.contains_privacy_data
            elif condition.startswith("complexity"):
                # 按规则字符串解析
                parts = condition.split(" and ")
                all_match = True
                for part in parts:
                    if "==" in part:
                        key, val = part.split("==", 1)
                        key = key.strip()
                        val = val.strip()
                        if key == "complexity":
                            if request.complexity != val:
                                all_match = False
                        elif key == "contains_privacy":
                            if request.contains_privacy_data == (val == "true"):
                                all_match = False
                if not all_match:
                    matched = False
                else:
                    matched = True
            elif condition == "default":
                matched = True

            if matched:
                primary = rule.get("provider", "local_alpha")
                fallback = rule.get("fallback", "none")
                # 如果用户配置了自定义 provider，优先使用（覆盖规则）
                if "user_configured" in self._providers:
                    primary = "user_configured"
                    # use_for=[] 表示支持所有任务类型
                    # fallback 仍按原规则（隐私任务不自动回退云端）
                    fallback = fallback if fallback in self._providers else "none"
                return (primary, fallback)

        # 默认：如有用户配置则优先
        if "user_configured" in self._providers:
            return ("user_configured", "none")
        return ("local_alpha", "none")


# 全局单例
_config_loader = _VE4AiConfigLoader()


# ════════════════════════════════════════════════════════════════
# 缓存
# ════════════════════════════════════════════════════════════════

class _VE4AiCache:
    """AI 调用缓存（按 prompt hash）"""

    def __init__(self):
        self._store: Dict[str, str] = {}

    def get(self, system: str, prompt: str, max_tokens: int) -> Optional[str]:
        key = hashlib.md5(
            (system + prompt + str(max_tokens)).encode()
        ).hexdigest()[:16]
        return self._store.get(key)

    def put(self, system: str, prompt: str, max_tokens: int, response: str):
        key = hashlib.md5(
            (system + prompt + str(max_tokens)).encode()
        ).hexdigest()[:16]
        self._store[key] = response

    def clear(self):
        self._store.clear()


_cache = _VE4AiCache()


# ════════════════════════════════════════════════════════════════
# 核心调用函数
# ════════════════════════════════════════════════════════════════

def ve4_ai_call(
    task_type: str,
    system: str = "",
    prompt: str = "",
    max_tokens: int = 0,
    temperature: float = 0.0,
    format_type: str = "text",
    contains_privacy_data: bool = False,
    complexity: str = "low",
    force_provider: str = "",
    require_explanation: bool = False,
) -> VE4AiResult:
    """
    统一 AI 调用入口。

    Args:
        task_type: 任务类型（必须在配置的 use_for 中注册）
        system: 系统指令
        prompt: 用户输入
        max_tokens: 最大输出 token（0=provider默认）
        temperature: 温度（0=provider默认）
        format_type: 输出格式 "text" / "json"
        contains_privacy_data: 是否含隐私数据
        complexity: 复杂度 "low" / "medium" / "high"
        force_provider: 强制使用指定 provider（空=自动路由）
        require_explanation: 是否要求返回推理过程

    Returns:
        VE4AiResult（含 provider、耗时、置信度）
    """
    start = time.time()
    request = VE4AiRequest(
        task_type=task_type,
        system=system,
        prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        format_type=format_type,
        contains_privacy_data=contains_privacy_data,
        complexity=complexity,
        force_provider=force_provider,
    )

    # ── Step 1: 加载配置 ──
    _config_loader.load()

    # ── Step 2: 确定 provider ──
    provider_name = ""
    fallback_name = "none"

    if force_provider:
        provider_name = force_provider
        if _config_loader.get_provider(provider_name):
            fallback_name = "none"
        else:
            return VE4AiResult(
                success=False,
                error=f"强制指定的 provider '{provider_name}' 未在配置中定义",
                duration_ms=int((time.time() - start) * 1000),
            )
    else:
        provider_name, fallback_name = _config_loader.resolve_route(request)

    # ── Step 3: 尝试主 provider ──
    result = _try_provider(request, provider_name, start)

    # ── Step 4: 尝试回退 ──
    if not result.success and fallback_name and fallback_name != "none":
        logger.info(f"[AI-GATEWAY] 主 provider '{provider_name}' 失败，回退到 '{fallback_name}'")
        result = _try_provider(request, fallback_name, start)

    return result


def _try_provider(request: VE4AiRequest, provider_name: str, start_time: float) -> VE4AiResult:
    """尝试用一个 provider 执行调用"""
    provider = _config_loader.get_provider(provider_name)
    if not provider:
        return VE4AiResult(
            success=False,
            error=f"provider '{provider_name}' 未配置",
            duration_ms=int((time.time() - start_time) * 1000),
        )

    # 检查任务类型是否被允许
    if provider.use_for and request.task_type not in provider.use_for:
        return VE4AiResult(
            success=False,
            error=f"任务类型 '{request.task_type}' 不在 provider '{provider_name}' 的 use_for 中",
            duration_ms=int((time.time() - start_time) * 1000),
        )

    # 格式化参数（0=使用 provider 默认值）
    max_tokens = request.max_tokens if request.max_tokens > 0 else provider.max_tokens
    temperature = request.temperature if request.temperature > 0 else provider.temperature
    format_type = request.format_type

    # 缓存检查
    if provider.cache_enabled:
        cached = _cache.get(request.system, request.prompt, max_tokens)
        if cached is not None:
            logger.debug(f"[AI-GATEWAY] 缓存命中：{provider_name}")
            return VE4AiResult(
                success=True,
                text=cached,
                provider=provider_name,
                model=provider.model,
                confidence=1.0,
                duration_ms=0,
                cached=True,
            )

    # 执行调用
    try:
        if provider.type == "ollama":
            text, explanation, reasoning, is_truncated = _call_ollama(
                provider, request.system, request.prompt,
                max_tokens, temperature, format_type)
        elif provider.type == "openai_compatible":
            text, explanation, reasoning, is_truncated = _call_openai(
                provider, request.system, request.prompt,
                max_tokens, temperature, format_type)
        else:
            return VE4AiResult(
                success=False,
                error=f"不支持的 provider 类型：{provider.type}",
                duration_ms=int((time.time() - start_time) * 1000),
            )

        elapsed = int((time.time() - start_time) * 1000)
        logger.info(f"[AI-GATEWAY] {provider_name} 调用成功 [{elapsed}ms]: "
                    f"{text[:80] if text else '[content为空]'}"
                    f"{' [截断]' if is_truncated else ''}")

        # 写入缓存（只缓存有内容且未截断的回复）
        if provider.cache_enabled and text.strip() and not is_truncated:
            _cache.put(request.system, request.prompt, max_tokens, text)

        # 置信度：有明确答案时高，text为空/截断时低
        confidence = 0.9 if text.strip() and not is_truncated else 0.0

        return VE4AiResult(
            success=True,
            text=text,
            provider=provider_name,
            model=provider.model,
            confidence=confidence,
            duration_ms=elapsed,
            explanation=explanation,
            reasoning=reasoning,
            is_truncated=is_truncated,
        )

    except Exception as e:
        elapsed = int((time.time() - start_time) * 1000)
        logger.warning(f"[AI-GATEWAY] {provider_name} 调用失败 [{elapsed}ms]: {e}")
        return VE4AiResult(
            success=False,
            error=str(e),
            provider=provider_name,
            model=provider.model,
            duration_ms=elapsed,
        )


def _call_with_retry(url: str, payload: dict, headers: dict = None,
                     timeout: int = 60, max_retries: int = 2) -> dict:
    """带重试的 HTTP POST 调用（超时和连接错误自动重试，4xx 不重试）"""
    import requests
    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers or {}, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout as e:
            last_exc = e
            logger.warning(f"[AI-GATEWAY] 请求超时 (第{attempt}/{max_retries}次): {url}")
        except requests.exceptions.ConnectionError as e:
            last_exc = e
            logger.warning(f"[AI-GATEWAY] 连接失败 (第{attempt}/{max_retries}次): {url}")
        except requests.exceptions.HTTPError as e:
            # 4xx 客户端错误不重试，直接抛出
            if resp is not None and 400 <= resp.status_code < 500:
                raise
            last_exc = e
            logger.warning(f"[AI-GATEWAY] HTTP错误 (第{attempt}/{max_retries}次): {resp.status_code}")
    raise last_exc


def _call_ollama(provider: VE4AiProvider, system: str, prompt: str,
                 max_tokens: int, temperature: float, format_type: str) -> tuple:
    """调用本地 Ollama 服务"""
    import requests

    url = _OLLAMA_CHAT_URL_TEMPLATE.format(base_url=provider.base_url.rstrip("/"))
    payload = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": system or "你是一个专业的金融助手。"},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {
            "num_predict": max_tokens,
            "temperature": temperature,
            "top_p": 0.9,
        }
    }

    if format_type == "json":
        payload["format"] = "json"

    data = _call_with_retry(url, payload, timeout=provider.timeout)

    content = data["choices"][0]["message"]["content"].strip()
    explanation = f"本地模型 {provider.model}，温度 {temperature}，最大 {max_tokens} tokens"

    # Ollama 不是推理模型，返回空 reasoning 和 is_truncated=False
    return content, explanation, "", False


def _call_openai(provider: VE4AiProvider, system: str, prompt: str,
                 max_tokens: int, temperature: float, format_type: str) -> tuple:
    """调用 OpenAI 兼容 API

    返回: (content, explanation, reasoning, is_truncated)
    - content: 模型正式输出
    - explanation: 调用元信息
    - reasoning: 推理过程（仅推理模型有值）
    - is_truncated: 输出是否被截断
    """
    import requests

    url = _OPENAI_CHAT_URL_TEMPLATE.format(base_url=provider.base_url.rstrip("/"))
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }

    # ── 推理模型适配：放大 token 预算 ──
    is_reasoning = provider.is_reasoning_model
    actual_max_tokens = max_tokens
    if is_reasoning and provider.reasoning_reserve > 0:
        actual_max_tokens = max_tokens + provider.reasoning_reserve
        logger.debug(f"[AI-GATEWAY] 推理模型 {provider.model}: max_tokens {max_tokens}→{actual_max_tokens} (reserve={provider.reasoning_reserve})")

    payload = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": system or "你是一个专业的金融助手。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": actual_max_tokens,
        "temperature": temperature,
    }

    # ── JSON 格式处理 ──
    if format_type == "json":
        if not is_reasoning:
            # 普通模型：强制 JSON 输出（行业标准）
            payload["response_format"] = {"type": "json_object"}
            json_instruction = "\n\n重要：你必须且只能返回合法的JSON格式数据，不要包含任何解释、markdown代码块标记或额外文字。"
            if system:
                payload["messages"][0]["content"] = system + json_instruction
            else:
                payload["messages"][0]["content"] = "你是一个专业的金融助手。" + json_instruction
        else:
            # 推理模型：不强制 response_format（推理阶段不受约束，强制反而可能导致 content 为空）
            # 仅在 system prompt 中要求 JSON 格式
            json_instruction = "\n\n重要：你必须且只能返回合法的JSON格式数据，不要包含任何解释、markdown代码块标记或额外文字。"
            if system:
                payload["messages"][0]["content"] = system + json_instruction
            else:
                payload["messages"][0]["content"] = "你是一个专业的金融助手。" + json_instruction

    data = _call_with_retry(url, payload, headers=headers, timeout=provider.timeout)

    logger.debug(f"[AI-GATEWAY] OpenAI API raw response keys: {list(data.keys())}")
    if "choices" in data and data["choices"]:
        msg = data["choices"][0].get("message", {})
        logger.debug(f"[AI-GATEWAY] message keys: {list(msg.keys())}")

    # ── 响应解析（归一化层）──
    content = ""
    reasoning = ""
    is_truncated = False

    if "choices" in data and data["choices"]:
        msg = data["choices"][0].get("message", {})
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()

        # 检查 finish_reason 判断是否被截断
        finish_reason = data["choices"][0].get("finish_reason", "")
        if finish_reason == "length":
            is_truncated = True
            logger.info(f"[AI-GATEWAY] finish_reason=length, 输出被截断")

        # ── content 为空时的归一化处理 ──
        if not content and reasoning:
            logger.info(f"[AI-GATEWAY] content为空，reasoning_content有{len(reasoning)}字符")

            if is_reasoning:
                # 推理模型：content 为空 = token 被推理耗尽
                # 不从 reasoning 暴力提取 JSON（reasoning 中的 JSON 不可信）
                # 对 text 格式：取 reasoning 尾部作为输出（保持兼容）
                if format_type and "json" in format_type.lower():
                    # JSON 格式：标记截断，让下游 repair 处理
                    is_truncated = True
                    logger.info("[AI-GATEWAY] 推理模型JSON输出为空，标记为截断")
                else:
                    # text 格式：从 reasoning 提取有效答案段
                    markers = ['最终输出', '最终答案', '最终', '总结：', '因此', '所以', '答案如下', '如下：']
                    best_start = 0
                    for m in markers:
                        idx = reasoning.rfind(m)
                        if idx > len(reasoning) * 0.3:
                            best_start = max(best_start, idx)
                            break
                    if best_start > 0:
                        content = reasoning[best_start:].strip()
                        logger.info(f"[AI-GATEWAY] 从reasoning答案段提取 ({len(content)}字符)")
                    else:
                        content = reasoning[-3000:].strip()
                        logger.info(f"[AI-GATEWAY] 从reasoning尾部3000字符提取 ({len(content)}字符)")
            else:
                # 非推理模型但 content 为空（异常情况）：尝试从 reasoning 提取
                if format_type and "json" in format_type.lower():
                    import re as _re
                    candidates = _re.findall(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', reasoning, _re.DOTALL)
                    best = ""
                    for c in candidates:
                        try:
                            json.loads(c)
                            if len(c) > len(best):
                                best = c
                        except Exception:
                            pass
                    if not best:
                        json_match = _re.search(r'\{[\s\S]*\}', reasoning)
                        if json_match:
                            try:
                                json.loads(json_match.group(0))
                                best = json_match.group(0)
                            except Exception:
                                pass
                    if best:
                        content = best
                        logger.info(f"[AI-GATEWAY] 非推理模型从reasoning提取JSON ({len(content)}字符)")
                    else:
                        is_truncated = True

        # ── content 存在但可能截断的检测（仅 JSON 格式）──
        elif content and format_type and "json" in format_type.lower():
            # 检测 JSON 括号是否平衡
            brace_diff = content.count('{') - content.count('}')
            bracket_diff = content.count('[') - content.count(']')
            if brace_diff > 0 or bracket_diff > 0:
                is_truncated = True
                logger.info(f"[AI-GATEWAY] JSON括号不平衡({{差={brace_diff}, [差={bracket_diff})，标记为截断")

        # 如果都为空，记录完整 message 结构用于调试
        if not content:
            logger.warning(f"[AI-GATEWAY] API返回content为空，message结构: {json.dumps(msg, ensure_ascii=False)[:300]}")

    usage = data.get("usage", {})
    explanation = (f"云端模型 {provider.model}{' [推理]' if is_reasoning else ''}，温度 {temperature}，"
                   f"输入 {usage.get('prompt_tokens', '?')} tokens，"
                   f"输出 {usage.get('completion_tokens', '?')} tokens")

    return content, explanation, reasoning, is_truncated


# ════════════════════════════════════════════════════════════════
# 便捷调用函数
# ════════════════════════════════════════════════════════════════

def ve4_ai_ask_yesno(question: str, contains_privacy: bool = False) -> Optional[bool]:
    """
    二分类判断：是/否/不确定。

    Args:
        question: 问题（如 "这份文件是银行账单吗？"）
        contains_privacy: 问题是否涉及隐私数据

    Returns:
        True / False / None（不确定）
    """
    system = "你是一个判断助手。根据用户的问题，只回答 yes 或 no 或 unknown，不要输出其他内容。"
    result = ve4_ai_call(
        task_type="yes_no_question",
        system=system,
        prompt=question,
        max_tokens=5,
        temperature=0.1,
        contains_privacy_data=contains_privacy,
        complexity="low",
    )

    if not result.success:
        return None

    text = result.text.strip().lower()
    if "yes" in text or "是" in text:
        return True
    if "no" in text or "不" in text or "否" in text:
        return False
    return None


def ve4_ai_ask_choice(
    question: str,
    choices: list,
    task_type: str = "classification_helper",
    contains_privacy: bool = False,
) -> Optional[str]:
    """
    多选一判断。

    Args:
        question: 问题
        choices: 选项列表
        task_type: 任务类型
        contains_privacy: 是否含隐私数据

    Returns:
        选中的选项 / None（不确定）
    """
    choices_str = ", ".join([f"\"{c}\"" for c in choices])
    system = (f"你是一个分类助手。只从以下选项中选择一个输出：{choices_str}。"
              "不要解释，不要输出其他内容。如果不确定，输出 \"unknown\"。")

    result = ve4_ai_call(
        task_type=task_type,
        system=system,
        prompt=question,
        max_tokens=10,
        temperature=0.1,
        contains_privacy_data=contains_privacy,
        complexity="low",
    )

    if not result.success:
        return None

    text = result.text.strip().strip('"').strip("'")
    if text in choices:
        return text
    if text == "unknown":
        return None
    # 模糊匹配
    for c in choices:
        if c in text:
            return c
    return None


# ════════════════════════════════════════════════════════════════
# 管理与诊断接口
# ════════════════════════════════════════════════════════════════

def ve4_ai_reload_config():
    """强制重新加载配置文件（配置变更后调用）"""
    _config_loader.load(force=True)
    _cache.clear()
    logger.info("[AI-GATEWAY] 配置已重新加载，缓存已清空")


def ve4_ai_get_providers() -> Dict[str, dict]:
    """获取当前所有 provider 的摘要信息（用于诊断和前端展示）"""
    _config_loader.load()
    result = {}
    for name, p in _config_loader._providers.items():
        result[name] = {
            "type": p.type,
            "model": p.model,
            "base_url": p.base_url,
            "timeout": p.timeout,
            "priority": p.priority,
            "cache_enabled": p.cache_enabled,
            "use_for": p.use_for,
            "has_api_key": bool(p.api_key),
        }
    return result


def ve4_ai_get_defaults() -> dict:
    """获取默认参数"""
    _config_loader.load()
    return dict(_config_loader._defaults)


def ve4_ai_health_check(provider_name: str = "") -> Dict[str, Any]:
    """
    AI 配置中心健康检查。

    Args:
        provider_name: 指定 provider，空=检查所有

    Returns:
        {provider_name: {"status": "ok"/"error", "latency_ms": int, "error": ""}}
    """
    _config_loader.load()
    results = {}

    providers_to_check = [provider_name] if provider_name else list(_config_loader._providers.keys())

    for name in providers_to_check:
        provider = _config_loader.get_provider(name)
        if not provider:
            results[name] = {"status": "error", "error": "未配置"}
            continue

        start = time.time()
        try:
            if provider.type == "ollama":
                # 用 /api/tags 做轻量检查
                import requests
                resp = requests.get(
                    f"{provider.base_url}/api/tags",
                    timeout=3,
                )
                ok = resp.status_code == 200
            elif provider.type == "openai_compatible":
                import requests
                headers = {"Authorization": f"Bearer {provider.api_key}"}
                resp = requests.get(
                    f"{provider.base_url}/models",
                    headers=headers,
                    timeout=5,
                )
                ok = resp.status_code == 200
            else:
                ok = False

            elapsed = int((time.time() - start) * 1000)
            results[name] = {
                "status": "ok" if ok else "error",
                "latency_ms": elapsed,
                "error": "" if ok else f"HTTP {resp.status_code}",
            }
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            results[name] = {
                "status": "error",
                "latency_ms": elapsed,
                "error": str(e),
            }

    return results


# ════════════════════════════════════════════════════════════════
# 多模态视觉调用（OCR 回退方案）
# ════════════════════════════════════════════════════════════════

def ve4_ai_extract_text_from_image(
    image_path: str,
    system: str = "",
    prompt: str = "",
    contains_privacy_data: bool = False,
    force_provider: str = "",
) -> VE4AiResult:
    """
    从图片中提取文字（多模态视觉调用）。

    用于本地 OCR（pytesseract/easyocr）不可用时的回退方案。
    支持的 provider：
        - ollama: 需要 llava 等视觉模型
        - openai_compatible: 需要 gpt-4o / gpt-4o-mini 等视觉模型

    Args:
        image_path: 图片文件路径
        system: 系统指令（默认为金融截图识别专用）
        prompt: 额外提示（默认为空）
        contains_privacy_data: 是否含隐私数据
        force_provider: 强制指定 provider

    Returns:
        VE4AiResult（text 为提取的文字）
    """
    start = time.time()
    img_file = Path(image_path)

    if not img_file.exists():
        return VE4AiResult(
            success=False, error=f"图片不存在：{image_path}",
            duration_ms=int((time.time() - start) * 1000),
        )

    # 读取图片并编码为 base64
    import base64
    img_bytes = img_file.read_bytes()
    b64 = base64.b64encode(img_bytes).decode("utf-8")

    # 判断图片格式
    suffix = img_file.suffix.lower().lstrip(".")
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "bmp": "image/bmp", "gif": "image/gif"}
    mime = mime_map.get(suffix, "image/png")
    data_url = f"data:{mime};base64,{b64}"

    # 默认系统指令
    if not system:
        system = (
            "你是一个专业的金融截图识别助手。"
            "请仔细识别图片中的所有文字，按原始布局输出，不要遗漏任何信息。"
            "保留数字、金额、百分比、日期等精确数值。"
        )
    if not prompt:
        prompt = "请完整提取这张图片中的所有文字内容。"

    _config_loader.load()

    # 确定 provider
    provider_name = ""
    fallback_name = "none"
    if force_provider:
        provider_name = force_provider
        if not _config_loader.get_provider(provider_name):
            return VE4AiResult(
                success=False, error=f"provider '{provider_name}' 未定义",
                duration_ms=int((time.time() - start) * 1000),
            )
        fallback_name = "none"
    else:
        # 路由：含隐私数据 → 本地优先（无回退）；不含隐私 → 云端优先，回退本地
        if contains_privacy_data:
            provider_name = "local_alpha"
            fallback_name = "none"
        else:
            provider_name, fallback_name = "cloud_beta", "local_alpha"

    result = _try_vision_provider(provider_name, system, prompt, data_url, mime, start)

    if not result.success and fallback_name and fallback_name != "none":
        logger.info(f"[AI-VISION] 主 provider '{provider_name}' 失败，回退到 '{fallback_name}'")
        result = _try_vision_provider(fallback_name, system, prompt, data_url, mime, start)

    return result


def _try_vision_provider(provider_name: str, system: str, prompt: str,
                          data_url: str, mime: str, start_time: float) -> VE4AiResult:
    """用单个 provider 执行视觉调用"""
    provider = _config_loader.get_provider(provider_name)
    if not provider:
        return VE4AiResult(
            success=False, error=f"provider '{provider_name}' 未配置",
            duration_ms=int((time.time() - start_time) * 1000),
        )

    # 检查是否支持 vision 任务
    if provider.use_for and "vision_ocr" not in provider.use_for and "vision" not in provider.use_for:
        return VE4AiResult(
            success=False,
            error=f"provider '{provider_name}' 未注册 vision_ocr 任务",
            duration_ms=int((time.time() - start_time) * 1000),
        )

    try:
        if provider.type == "ollama":
            text, explanation = _call_ollama_vision(provider, system, prompt, data_url)
        elif provider.type == "openai_compatible":
            text, explanation = _call_openai_vision(provider, system, prompt, data_url)
        else:
            return VE4AiResult(
                success=False, error=f"不支持的 provider 类型：{provider.type}",
                duration_ms=int((time.time() - start_time) * 1000),
            )

        elapsed = int((time.time() - start_time) * 1000)
        logger.info(f"[AI-VISION] {provider_name} 视觉识别成功 [{elapsed}ms]: {text[:60]}...")

        return VE4AiResult(
            success=True, text=text,
            provider=provider_name, model=provider.model,
            confidence=0.85, duration_ms=elapsed,
            explanation=explanation,
        )

    except Exception as e:
        elapsed = int((time.time() - start_time) * 1000)
        logger.warning(f"[AI-VISION] {provider_name} 视觉识别失败 [{elapsed}ms]: {e}")
        return VE4AiResult(
            success=False, error=str(e),
            provider=provider_name, model=provider.model,
            duration_ms=elapsed,
        )


def _call_ollama_vision(provider: VE4AiProvider, system: str,
                         prompt: str, data_url: str) -> tuple:
    """Ollama 多模态调用（需要 llava 等视觉模型）"""
    import requests
    url = _OLLAMA_CHAT_URL_TEMPLATE.format(base_url=provider.base_url.rstrip("/"))
    payload = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ]},
        ],
        "stream": False,
        "options": {"num_predict": provider.max_tokens, "temperature": 0.1},
    }
    data = _call_with_retry(url, payload, timeout=60)
    content = data["choices"][0]["message"]["content"].strip()
    return content, f"Ollama 视觉模型 {provider.model}"


def _call_openai_vision(provider: VE4AiProvider, system: str,
                         prompt: str, data_url: str) -> tuple:
    """OpenAI 兼容多模态调用（需要 gpt-4o 等视觉模型）"""
    import requests
    url = _OPENAI_CHAT_URL_TEMPLATE.format(base_url=provider.base_url.rstrip("/"))
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }
    payload = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ]},
        ],
        "max_tokens": provider.max_tokens * 4,  # 视觉模型输出较长，给 4 倍
        "temperature": 0.1,
    }
    data = _call_with_retry(url, payload, headers=headers, timeout=60)
    content = data["choices"][0]["message"]["content"].strip()
    usage = data.get("usage", {})
    explanation = (f"云端视觉模型 {provider.model}，"
                   f"输入 {usage.get('prompt_tokens', '?')} tokens，"
                   f"输出 {usage.get('completion_tokens', '?')} tokens")
    return content, explanation


# ─── 旧名别名（向后兼容）───
ai_call = ve4_ai_call
reload_config = ve4_ai_reload_config
health_check = ve4_ai_health_check
