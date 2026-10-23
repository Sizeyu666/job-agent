"""打招呼话术生成（P4）。

输入：岗位 JD 摘要 + 匹配点 + 切入点 + 结构化简历；
输出：3 条不同风格的招呼语（direct 项目直击 / skill 技能匹配 / sincere 诚意请教）。

硬性约束在 prompts.GREETING_USER 里已写明，本模块负责生成后的自检：
超长、命中站外联系方式词、开头雷同、风格缺失都会触发重新生成（最多 2 轮），
仍不合格的条目直接丢弃——宁可少一条，也不把违规话术给用户去发。
"""

from __future__ import annotations

import re

from loguru import logger

from core.config import load_profile
from core.db import (
    get_active_resume,
    get_evaluation,
    get_job,
    jobs_needing_greeting,
    replace_greetings,
)
from core.models import Evaluation, Job, Resume
from core.prompts import GREETING_SYSTEM, GREETING_USER
from core.resume import build_resume_brief

# 三种风格（顺序即展示顺序）
STYLES = ("direct", "skill", "sincere")

# 除配置里的禁用词外，额外拦截的拉私聊话术
EXTRA_BANNED = ("加个", "加我", "私聊", "留个联系", "手机号", "电话联系")

# 开头雷同判定：取开头 6 个字比较
_OPENING_LEN = 6
_PUNCT = re.compile(r"[\s，。！？、；：“”‘’（）()【】\[\]~～·.!?,:;\"']+")


def _normalize(text: str) -> str:
    """压平空白与换行，去掉多余的引号包裹。"""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text.strip("\"'“”")


def _opening(text: str) -> str:
    """取开头若干字（去掉标点空白），用于判断三条开头是否雷同。"""
    return _PUNCT.sub("", text)[:_OPENING_LEN]


def _validate(items: list[dict], profile) -> tuple[list[dict], list[str]]:
    """自检话术，返回 (合格条目, 问题描述列表)。"""
    count = max(1, profile.greeting.count)
    max_length = max(60, profile.greeting.max_length)
    banned = [w for w in (profile.greeting.banned_words or []) + list(EXTRA_BANNED) if w]

    valid: list[dict] = []
    problems: list[str] = []
    openings: set[str] = set()

    for item in items:
        style = str(item.get("style") or "").strip()
        content = _normalize(item.get("content"))
        if style not in STYLES:
            problems.append(f"风格值非法：{style or '空'}")
            continue
        if not content:
            problems.append(f"{style} 内容为空")
            continue
        hits = [w for w in banned if w and w in content]
        if hits:
            problems.append(f"{style} 命中禁用词：{'、'.join(hits)}")
            continue
        if len(content) > max_length:
            problems.append(f"{style} 超长：{len(content)} 字（上限 {max_length}）")
            continue
        opening = _opening(content)
        if opening and opening in openings:
            problems.append(f"{style} 开头与其他条目雷同：{opening}")
            continue
        if style in {i["style"] for i in valid}:
            problems.append(f"{style} 重复出现")
            continue
        openings.add(opening)
        valid.append({"style": style, "content": content})

    if len(valid) < count:
        problems.append(f"只得到 {len(valid)} 条合格话术，需要 {count} 条")
    return valid[:count], problems


def _request_once(
    job: Job,
    resume: Resume,
    evaluation: Evaluation,
    profile,
    feedback: str = "",
) -> list[dict]:
    """调一次 LLM 生成话术（feedback 非空时把上一版问题带进 prompt 要求修正）。"""
    from core.llm import chat_json

    jd_summary = evaluation.jd_summary or (job.jd_text or "")[:200]
    user_prompt = GREETING_USER.format(
        job_title=job.title,
        company=job.company,
        jd_summary=jd_summary,
        pitch_angle=evaluation.pitch_angle or "技能与项目经历的匹配度",
        matched_points="；".join(evaluation.matched_points or []) or "无",
        resume_brief=build_resume_brief(resume),
        count=max(1, profile.greeting.count),
        max_length=max(60, profile.greeting.max_length),
    )
    if feedback:
        user_prompt += f"\n\n## 上一版的问题（必须全部修正）\n{feedback}"

    data = chat_json(
        GREETING_SYSTEM,
        user_prompt,
        temperature=profile.llm.greeting_temperature,
    )
    raw = data.get("greetings") or []
    return [{"style": str(i.get("style") or ""), "content": i.get("content") or ""} for i in raw if isinstance(i, dict)]


def generate_for_evaluation(job: Job, resume: Resume, evaluation: Evaluation) -> list[dict]:
    """为一个已打分岗位生成 3 条话术；两轮仍不达标时返回尽力得到的合格条目。"""
    profile = load_profile()
    best: list[dict] = []
    feedback = ""

    for attempt in (1, 2):
        try:
            items = _request_once(job, resume, evaluation, profile, feedback)
        except Exception as e:  # noqa: BLE001 - 单岗位生成失败不影响其它岗位
            logger.warning("岗位「{}」话术生成失败（第 {} 轮）：{}", job.title, attempt, e)
            return best

        valid, problems = _validate(items, profile)
        if len(valid) > len(best):
            best = valid
        if len(best) >= max(1, profile.greeting.count):
            logger.info("话术生成成功：{}｜{}｜{} 条",
                        job.title, job.company, len(best))
            return best
        feedback = "；".join(problems)
        logger.warning("岗位「{}」第 {} 轮话术不合格：{}", job.title, attempt, feedback)

    logger.warning("岗位「{}」话术最终只有 {} 条合格，已丢弃其余", job.title, len(best))
    return best


def generate_for_job(job_pk: int) -> dict:
    """为单个岗位重新生成话术（P5 详情页按钮用）。"""
    job = get_job(job_pk)
    if not job:
        raise RuntimeError(f"岗位不存在：{job_pk}")
    resume = get_active_resume()
    if not resume:
        raise RuntimeError("没有生效的简历，请先上传简历")
    evaluation = get_evaluation(job_pk, resume.id)
    if not evaluation:
        raise RuntimeError("该岗位还没有打分记录，请先打分")
    if evaluation.grade not in ("recommended", "candidate"):
        raise RuntimeError(f"该岗位评级为 {evaluation.grade}，不生成话术")

    items = generate_for_evaluation(job, resume, evaluation)
    if not items:
        return {"generated": 0, "items": []}
    replace_greetings(job_pk, resume.id, items)
    return {"generated": len(items), "items": items}


def generate_pending(limit: int = 50) -> dict:
    """给推荐/可考虑且还没有话术的岗位批量生成（流水线第 6 步）。"""
    resume = get_active_resume()
    if not resume:
        raise RuntimeError("没有生效的简历，请先上传简历")

    pairs = jobs_needing_greeting(resume.id, limit=limit)
    stats = {"total": len(pairs), "greeted": 0, "skipped": 0, "failed": 0}
    logger.info("待生成话术岗位 {} 个（简历 id={}）", len(pairs), resume.id)

    for job, evaluation in pairs:
        try:
            items = generate_for_evaluation(job, resume, evaluation)
            if not items:
                stats["skipped"] += 1
                continue
            replace_greetings(job.id, resume.id, items)
            stats["greeted"] += 1
        except Exception as e:  # noqa: BLE001 - 单岗位失败隔离
            stats["failed"] += 1
            logger.exception("岗位 {} 话术生成异常：{}", job.id, e)

    logger.info(
        "话术批量结束：共 {} 个，成功 {}，无合格话术 {}，失败 {}",
        stats["total"], stats["greeted"], stats["skipped"], stats["failed"],
    )
    return stats
