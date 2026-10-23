"""LLM 调用统一封装（OpenAI 兼容接口：DeepSeek / 豆包 / 通义等）。

- 统一超时、tenacity 重试、JSON 解析失败重试；
- token 用量写入日志；
- 密钥只从 secrets.yaml 读取。
"""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger
from openai import OpenAI
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.config import load_profile, load_secrets


class LLMError(RuntimeError):
    """LLM 调用或解析失败。"""


def get_client() -> OpenAI:
    """构造 OpenAI 兼容客户端（每次按最新配置创建，便于设置页热更新）。"""
    profile = load_profile()
    secrets = load_secrets()
    if not secrets.llm_api_key or not secrets.llm_api_key.startswith("sk-"):
        raise LLMError("未配置有效的 LLM API Key，请到「设置」页填写")
    return OpenAI(
        api_key=secrets.llm_api_key,
        base_url=profile.llm.base_url,
        timeout=profile.llm.timeout_seconds,
        max_retries=0,  # 重试交给 tenacity 统一控制
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=15),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _chat(
    messages: list[dict],
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
) -> str:
    """底层调用：失败自动重试 3 次（指数退避）。"""
    profile = load_profile()
    client = get_client()
    kwargs: dict[str, Any] = {
        "model": profile.llm.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens or profile.llm.max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    usage = getattr(resp, "usage", None)
    if usage:
        logger.debug(
            "LLM 用量：prompt={} completion={} total={}",
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
        )
    content = resp.choices[0].message.content or ""
    if not content.strip():
        raise LLMError("LLM 返回空内容")
    return content


def parse_json_text(text: str) -> dict:
    """从 LLM 输出中提取 JSON 对象（容忍 ```json 代码块和前后多余文字）。"""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=15),
    retry=retry_if_exception_type((json.JSONDecodeError, LLMError)),
    reraise=True,
)
def chat_json(
    system_prompt: str,
    user_prompt: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """要求 LLM 输出 JSON 对象并解析；解析失败自动重试。"""
    text = _chat(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        temperature=0.3 if temperature is None else temperature,
        max_tokens=max_tokens,
        json_mode=True,
    )
    try:
        return parse_json_text(text)
    except json.JSONDecodeError as e:
        logger.warning("LLM JSON 解析失败，将重试：{} | 原始输出前 200 字：{}", e, text[:200])
        raise


def chat_text(
    system_prompt: str,
    user_prompt: str,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """普通文本对话（话术生成等）。"""
    return _chat(
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
        temperature=0.8 if temperature is None else temperature,
        max_tokens=max_tokens,
        json_mode=False,
    )


def test_llm() -> tuple[bool, str]:
    """设置页「测试 LLM 连通」：发一条极短请求验证 key/base_url/model。"""
    try:
        text = chat_text("你是连通性测试助手，只回复 ok。", "请回复 ok", temperature=0, max_tokens=10)
        return True, text.strip()[:50]
    except Exception as e:  # noqa: BLE001 - 测试按钮需要把错误原样展示给用户
        logger.warning("LLM 连通测试失败：{}", e)
        return False, str(e)
