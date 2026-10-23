"""配置加载与保存：profile.yaml（业务配置）与 secrets.yaml（密钥）。

- 启动时用 pydantic 校验 profile.yaml；
- 网页「设置」页保存后调用 load_profile(force_reload=True) 实现热更新；
- 密钥只从 secrets.yaml 读取，禁止写死在代码里。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Literal, Optional

import yaml
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

# 项目根目录（core 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
PROFILE_PATH = CONFIG_DIR / "profile.yaml"
PROFILE_EXAMPLE = CONFIG_DIR / "profile.example.yaml"
SECRETS_PATH = CONFIG_DIR / "secrets.yaml"
SECRETS_EXAMPLE = CONFIG_DIR / "secrets.example.yaml"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"
RESUME_DIR = PROJECT_ROOT / "resumes"


# ------------------------- pydantic 模型 -------------------------
class CityConfig(BaseModel):
    """城市配置：BOSS 城市码。"""

    name: str
    code: str


class ScheduleConfig(BaseModel):
    """定时任务配置，cron 格式「分 时 日 月 周」。"""

    cron: list[str] = Field(default_factory=lambda: ["30 9 * * *", "30 14 * * *"])


class LLMConfig(BaseModel):
    """LLM（OpenAI 兼容接口）配置。"""

    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = 0.3
    greeting_temperature: float = 0.8
    max_tokens: int = 2000
    timeout_seconds: int = 60


class EmbeddingConfig(BaseModel):
    """本地向量模型配置。"""

    model_name: str = "BAAI/bge-small-zh-v1.5"
    cache_dir: str = "data/models"


class WeightsConfig(BaseModel):
    """最终分数权重。"""

    embedding: float = 0.35
    llm: float = 0.65


class GreetingConfig(BaseModel):
    """打招呼话术配置。"""

    count: int = 3
    max_length: int = 280
    banned_words: list[str] = Field(
        default_factory=lambda: ["微信", "加我", "vx", "VX", "Vx", "v信", "私聊", "QQ", "qq"]
    )


class NotifyConfig(BaseModel):
    """推送渠道开关。"""

    enabled: dict[str, bool] = Field(
        default_factory=lambda: {"feishu": False, "serverchan": False}
    )


class WebConfig(BaseModel):
    """Web 服务配置。"""

    host: str = "127.0.0.1"
    port: int = 8000


class Profile(BaseModel):
    """profile.yaml 对应的完整业务配置。"""

    target_keywords: list[str] = Field(min_length=1)
    cities: list[CityConfig] = Field(min_length=1)
    salary_min_k: Optional[int] = None
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    blacklist_companies: list[str] = Field(default_factory=list)
    score_threshold: int = 75
    hard_filter_enabled: bool = True
    per_keyword_max_pages: int = 2
    detail_page_limit: int = 60
    browser_headless: bool = False
    # manual：只用书签/手动粘贴入库（BOSS 会拦截程序驱动浏览器，默认走这条）
    # browser：旧的 Playwright 采集（BOSS 正常时可用）
    collect_mode: Literal["manual", "browser"] = "manual"
    min_delay_seconds: int = 3
    max_delay_seconds: int = 8
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    weights: WeightsConfig = Field(default_factory=WeightsConfig)
    greeting: GreetingConfig = Field(default_factory=GreetingConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    web: WebConfig = Field(default_factory=WebConfig)


class Secrets(BaseModel):
    """secrets.yaml 对应的密钥配置。"""

    llm_api_key: str = ""
    feishu_webhook: str = ""
    serverchan_sendkey: str = ""


# ------------------------- 缓存 -------------------------
_profile_cache: Optional[Profile] = None
_secrets_cache: Optional[Secrets] = None


def ensure_config_files() -> None:
    """确保 config/profile.yaml 与 secrets.yaml 存在（不存在时从 example 复制）。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not PROFILE_PATH.exists() and PROFILE_EXAMPLE.exists():
        shutil.copyfile(PROFILE_EXAMPLE, PROFILE_PATH)
        logger.info("已从 profile.example.yaml 生成 config/profile.yaml")
    if not SECRETS_PATH.exists() and SECRETS_EXAMPLE.exists():
        shutil.copyfile(SECRETS_EXAMPLE, SECRETS_PATH)
        logger.info("已从 secrets.example.yaml 生成 config/secrets.yaml")


def ensure_dirs() -> None:
    """创建运行时目录。"""
    for d in (DATA_DIR, LOG_DIR, RESUME_DIR, DATA_DIR / "models", DATA_DIR / "browser_profile"):
        d.mkdir(parents=True, exist_ok=True)


def _read_yaml(path: Path) -> dict:
    """读取 YAML 文件为 dict，空文件返回空 dict。"""
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_profile(force_reload: bool = False) -> Profile:
    """加载并校验 profile.yaml，带进程内缓存；force_reload=True 时重新读取。"""
    global _profile_cache
    if _profile_cache is not None and not force_reload:
        return _profile_cache
    ensure_config_files()
    try:
        data = _read_yaml(PROFILE_PATH)
        _profile_cache = Profile(**data)
    except ValidationError as e:
        # 配置不合法时抛出明确错误，交由调用方（网页/命令行）提示用户修改
        logger.error("profile.yaml 校验失败：{}", e)
        raise
    return _profile_cache


def load_secrets(force_reload: bool = False) -> Secrets:
    """加载 secrets.yaml（不存在则返回空密钥）。"""
    global _secrets_cache
    if _secrets_cache is not None and not force_reload:
        return _secrets_cache
    ensure_config_files()
    data = _read_yaml(SECRETS_PATH) if SECRETS_PATH.exists() else {}
    _secrets_cache = Secrets(**{k: (v or "") for k, v in data.items() if k in Secrets.model_fields})
    return _secrets_cache


def save_profile(data: dict) -> Profile:
    """保存 profile.yaml 并热更新缓存，返回校验后的配置。"""
    global _profile_cache
    validate = Profile(**data)  # 先校验，校验失败抛异常且不写盘
    with PROFILE_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(validate.model_dump(), f, allow_unicode=True, sort_keys=False)
    _profile_cache = validate
    logger.info("profile.yaml 已更新并热加载")
    return validate


def save_secrets(data: dict) -> Secrets:
    """保存 secrets.yaml 并热更新缓存。"""
    global _secrets_cache
    validate = Secrets(**data)
    with SECRETS_PATH.open("w", encoding="utf-8") as f:
        yaml.safe_dump(validate.model_dump(), f, allow_unicode=True, sort_keys=False)
    _secrets_cache = validate
    logger.info("secrets.yaml 已更新并热加载")
    return validate
