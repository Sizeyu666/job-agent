"""内置匹配打分 skill：三层打分（硬过滤 → 向量相似度 + 技能覆盖 → LLM 语义分）。

设计要点：
1. 第 1 层是纯规则硬过滤，命中即淘汰，绝不调用 LLM（省钱、省时、避免误判）；
2. 第 2 层全部本地计算：FastEmbed 向量余弦 + 技能词覆盖率，零 API 成本；
3. 第 3 层才调 LLM，强制 JSON 输出，解析失败由 tenacity 重试；
4. final = weights.embedding*embedding_score + weights.llm*llm_score，按阈值分档。

参考开源项目 srbhr/Resume-Matcher 的向量相似度 + 关键词覆盖思路，但不引入 Qdrant 等重依赖。
"""

from __future__ import annotations

import re

from loguru import logger

from core.config import PROJECT_ROOT, load_profile
from core.db import (
    get_active_resume,
    jobs_for_rescore,
    jobs_without_evaluation,
    upsert_evaluation,
)
from core.models import Job, Resume
from core.prompts import SCORE_SYSTEM, SCORE_USER
from core.resume import build_resume_brief, parse_structured

# 学历档次（数字越小要求越高），用于判断岗位学历要求是否高于用户学历
EDUCATION_RANK = {"博士": 1, "硕士": 2, "研究生": 2, "本科": 3, "大专": 4, "专科": 4, "中专": 5, "高中": 5}

# 从经验要求里抽最低年限：「3-5年」→3，「5年以上」→5，「经验不限/在校/应届」→0
EXPERIENCE_PATTERN = re.compile(r"(\d+)\s*[-~至]\s*\d+\s*年|(\d+)\s*年以上")

# 技能词表：用于从 JD 里抽技能要求，算技能覆盖率（可按需补充）
SKILL_VOCAB = [
    "python", "java", "c++", "c#", "javascript", "typescript", "go", "rust", "kotlin", "swift",
    "unity", "unreal", "ue4", "ue5", "opengl", "opencv", "shader", "3d", "ar", "vr", "mr",
    "arkit", "arcore", "vuforia", "easyar", "slam", "three.js", "webgl", "blender", "maya",
    "pytorch", "tensorflow", "onnx", "opencv", "numpy", "pandas",
    "langchain", "llamaindex", "rag", "agent", "prompt", "lora", "sft", "finetune",
    "fastapi", "flask", "django", "docker", "kubernetes", "k8s", "linux", "git",
    "mysql", "redis", "mongodb", "postgresql", "sqlite",
    "faiss", "milvus", "chroma", "pinecone", "qdrant", "向量数据库", "向量检索",
    "大模型", "多模态", "llm", "transformer", "attention", "nlp", "cv", "ocr",
    "边缘计算", "嵌入式", "android", "ios", "flutter", "react", "vue", "node.js",
]

# FastEmbed 模型单例（懒加载，避免导入即下载模型）
_embedder = None
# 模型下载失败后置 True，避免每个岗位都重试几十秒（当前网络无法访问模型源时纯浪费）
_embedder_unavailable = False


class EmbeddingUnavailable(RuntimeError):
    """向量模型不可用（未安装 / 下载失败）。"""


def get_embedder():
    """懒加载本地向量模型，首次调用会下载并缓存到 data/models/。"""
    global _embedder
    if _embedder is not None:
        return _embedder
    try:
        from fastembed import TextEmbedding
    except Exception as e:  # noqa: BLE001
        raise EmbeddingUnavailable(f"未安装 fastembed：{e}") from e

    profile = load_profile()
    cache_dir = PROJECT_ROOT / profile.embedding.cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info("加载向量模型 {}（缓存目录 {}）", profile.embedding.model_name, cache_dir)
    _embedder = TextEmbedding(model_name=profile.embedding.model_name, cache_dir=str(cache_dir))
    return _embedder


def _cosine(a, b) -> float:
    """两个向量余弦相似度，返回 0-1（负值截断为 0）。"""
    import numpy as np

    va = np.asarray(a, dtype="float32")
    vb = np.asarray(b, dtype="float32")
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return max(0.0, min(1.0, float(np.dot(va, vb) / denom)))


def _lexical_score(resume_text: str, jd_text: str) -> float:
    """离线兜底相似度：字符二元组 Jaccard，0-100（向量模型不可用时使用）。"""
    def bigrams(text: str) -> set:
        clean = re.sub(r"[\s，。；、：（）()【】\[\]/,.:;!?·\-—_]+", "", text or "")
        return {clean[i : i + 2] for i in range(len(clean) - 1)}

    a, b = bigrams(resume_text), bigrams(jd_text)
    if not a or not b:
        return 0.0
    return round(len(a & b) / len(a | b) * 100, 1)


def embedding_score(resume_text: str, jd_text: str) -> float:
    """简历与 JD 的向量相似度（0-100）；模型不可用时降级为字符相似度。"""
    global _embedder_unavailable
    if not resume_text.strip() or not jd_text.strip():
        return 0.0
    if _embedder_unavailable:
        return _lexical_score(resume_text, jd_text)
    try:
        model = get_embedder()
        vectors = list(model.embed([resume_text[:4000], jd_text[:4000]]))
    except Exception as e:  # noqa: BLE001 - 向量模型失败不应阻断整轮打分
        _embedder_unavailable = True
        logger.warning("向量模型不可用，后续改用字符相似度兜底：{}", e)
        return _lexical_score(resume_text, jd_text)
    if len(vectors) < 2:
        return 0.0
    return round(_cosine(vectors[0], vectors[1]) * 100, 1)


def extract_skills(text: str) -> set[str]:
    """从文本里抽取命中的技能词（小写归一）。"""
    lowered = (text or "").lower()
    return {word for word in SKILL_VOCAB if word in lowered}


def skill_coverage(resume_text: str, jd_text: str) -> float:
    """技能覆盖率：JD 要求的技术词被简历覆盖的比例（0-100）。"""
    jd_skills = extract_skills(jd_text)
    if not jd_skills:
        return 0.0
    resume_skills = extract_skills(resume_text)
    return round(len(jd_skills & resume_skills) / len(jd_skills) * 100, 1)


def _min_experience_years(text: str) -> int:
    """从经验要求文本里取最低年限，无法判断返回 0（视为不限）。"""
    if not text:
        return 0
    if any(word in text for word in ("不限", "在校", "应届", "实习")):
        return 0
    match = EXPERIENCE_PATTERN.search(text)
    if not match:
        return 0
    value = match.group(1) or match.group(2)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _user_education_rank(resume: Resume, structured: dict) -> int:
    """用户最高学历档次（数字越小越强），判断不到返回 0（不限制）。"""
    text = f"{structured.get('education') or ''} {resume.raw_text[:2000]}"
    best = 0
    for word, rank in EDUCATION_RANK.items():
        if word in text and (best == 0 or rank < best):
            best = rank
    return best


def hard_filter(job: Job, resume: Resume, profile=None) -> tuple[bool, list[str]]:
    """第 1 层硬过滤：返回 (是否通过, 未满足的硬性条件列表)。"""
    profile = profile or load_profile()
    gaps: list[str] = []
    structured = parse_structured(resume)
    resume_text = resume.raw_text or ""
    jd_text = job.jd_text or ""
    full_text = f"{job.title} {jd_text}"

    if not profile.hard_filter_enabled:
        return True, gaps

    # 城市不符
    city_names = [c.name for c in profile.cities]
    if job.city and city_names and not any(name in job.city for name in city_names):
        gaps.append(f"城市不符：{job.city}（目标：{'/'.join(city_names)}）")

    # 学历要求高于用户学历
    user_rank = _user_education_rank(resume, structured)
    job_rank = 0
    for word, rank in EDUCATION_RANK.items():
        if word in (job.education or "") and (job_rank == 0 or rank < job_rank):
            job_rank = rank
    if user_rank and job_rank and job_rank < user_rank:
        gaps.append(f"学历不满足：要求{job.education}，候选人为本科及以下")

    # 经验要求过高的全职岗位（候选人是找实习/初级岗的学生）
    years = _min_experience_years(job.experience or "")
    if years >= 3 and "实习" in job.experience:
        gaps.append(f"经验要求不符：{job.experience}")
    elif years >= 3:
        gaps.append(f"经验要求不符：{job.experience}（候选人为学生/实习）")

    # 排除关键词（标题或 JD 命中即淘汰）
    for word in profile.exclude_keywords:
        if word and word in full_text:
            gaps.append(f"命中排除关键词：{word}")

    # 公司黑名单
    for company in profile.blacklist_companies:
        if company and company in (job.company or ""):
            gaps.append(f"公司在黑名单：{company}")

    # 必备技能：JD 明确要求但简历完全没有
    lowered_resume = resume_text.lower()
    for skill in profile.must_have:
        if skill and skill.lower() in jd_text.lower() and skill.lower() not in lowered_resume:
            gaps.append(f"缺少必备技能：{skill}")

    return (not gaps), gaps


def _grade(final_score: float, threshold: float) -> str:
    """按最终分档：≥阈值推荐，50-阈值可考虑，其余淘汰。"""
    if final_score >= threshold:
        return "recommended"
    if final_score >= 50:
        return "candidate"
    return "rejected"


def score_job(job: Job, resume: Resume) -> dict:
    """对单个岗位打分，返回可直接写入 Evaluation 的字段 dict。"""
    profile = load_profile()
    structured = parse_structured(resume)

    # 第 1 层：硬过滤（命中即淘汰，不调 LLM）
    passed, gaps = hard_filter(job, resume, profile)
    if not passed:
        return {
            "hard_passed": False,
            "hard_gaps": gaps,
            "embedding_score": 0.0,
            "skill_coverage": 0.0,
            "llm_score": 0.0,
            "final_score": 0.0,
            "matched_points": [],
            "pitch_angle": "",
            "risk_flags": [],
            "jd_summary": "硬性条件不满足，未进入语义打分",
            "grade": "rejected",
        }

    # 第 2 层：本地向量相似度 + 技能覆盖率
    emb = embedding_score(resume.raw_text, job.jd_text)
    coverage = skill_coverage(resume.raw_text, job.jd_text)

    # 第 3 层：LLM 语义打分
    brief = build_resume_brief(resume)
    user_prompt = SCORE_USER.format(
        resume_brief=brief,
        job_title=job.title,
        company=job.company,
        city=job.city,
        salary=job.salary_raw,
        experience=job.experience,
        education=job.education,
        tags="、".join(job.tags or []) or "无",
        jd_text=(job.jd_text or "")[:6000],
    )

    llm_score = 0.0
    matched: list[str] = []
    risk_flags: list[str] = []
    pitch_angle = ""
    jd_summary = ""
    llm_gaps: list[str] = []
    llm_ok = True

    try:
        from core.llm import chat_json

        data = chat_json(SCORE_SYSTEM, user_prompt, temperature=0.2)
        llm_score = float(data.get("llm_score") or 0)
        llm_score = max(0.0, min(100.0, llm_score))
        matched = [str(x) for x in (data.get("matched_points") or [])][:5]
        risk_flags = [str(x) for x in (data.get("risk_flags") or [])][:5]
        pitch_angle = str(data.get("pitch_angle") or "")[:120]
        jd_summary = str(data.get("jd_summary") or "")[:120]
        if data.get("hard_qualified") is False:
            llm_gaps = [str(x) for x in (data.get("hard_gaps") or [])][:5] or ["LLM 判断硬性条件不满足"]
    except Exception as e:  # noqa: BLE001 - 单岗位 LLM 失败降级为向量分，不阻断整批
        llm_ok = False
        logger.warning("岗位 {} LLM 打分失败，降级为向量分：{}", job.id, e)

    # LLM 判定硬条件不合格：一票否决
    if llm_gaps:
        return {
            "hard_passed": False,
            "hard_gaps": llm_gaps,
            "embedding_score": emb,
            "skill_coverage": coverage,
            "llm_score": llm_score,
            "final_score": round(emb * 0.5, 1),
            "matched_points": matched,
            "pitch_angle": pitch_angle,
            "risk_flags": risk_flags,
            "jd_summary": jd_summary or "LLM 判断硬性条件不满足",
            "grade": "rejected",
        }

    if llm_ok:
        if _embedder_unavailable:
            # 向量模型不可用时，字符相似度量纲偏低会拖垮总分，把 embedding 权重整体让给 LLM
            final = llm_score
        else:
            final = (
                profile.weights.embedding * emb + profile.weights.llm * llm_score
            )
    else:
        # LLM 不可用时只按向量分排序，避免把岗位全判成 0 分
        final = emb

    final = round(final, 1)
    return {
        "hard_passed": True,
        "hard_gaps": [],
        "embedding_score": emb,
        "skill_coverage": coverage,
        "llm_score": llm_score,
        "final_score": final,
        "matched_points": matched,
        "pitch_angle": pitch_angle,
        "risk_flags": risk_flags,
        "jd_summary": jd_summary,
        "grade": _grade(final, profile.score_threshold),
    }


def _score_batch(jobs: list[Job], resume: Resume) -> dict:
    """批量打分并写库，返回统计结果。"""
    profile = load_profile()
    stats = {"total": len(jobs), "recommended": 0, "candidate": 0, "rejected": 0, "failed": 0}
    for job in jobs:
        try:
            data = score_job(job, resume)
            upsert_evaluation(job.id, resume.id, data)
            stats[data["grade"]] = stats.get(data["grade"], 0) + 1
            logger.info(
                "打分完成：{}｜{}｜{:.1f} 分（{}）",
                job.title, job.company, data["final_score"], data["grade"],
            )
        except Exception as e:  # noqa: BLE001 - 单岗位失败隔离，继续下一个
            stats["failed"] += 1
            logger.exception("岗位 {} 打分失败：{}", job.id, e)
    logger.info(
        "批量打分结束：共 {} 个，推荐 {}，可考虑 {}，淘汰 {}，失败 {}（阈值 {}）",
        stats["total"], stats["recommended"], stats["candidate"],
        stats["rejected"], stats["failed"], profile.score_threshold,
    )
    return stats


def score_pending(limit: int = 200) -> dict:
    """给当前生效简历尚未打分的岗位打分。"""
    resume = get_active_resume()
    if not resume:
        raise RuntimeError("没有生效的简历，请先上传简历")
    jobs = jobs_without_evaluation(resume.id, limit=limit)
    logger.info("待打分岗位 {} 个（简历 id={}）", len(jobs), resume.id)
    return _score_batch(jobs, resume)


def rescore_all(limit: int = 500) -> dict:
    """用当前生效简历重算全部岗位分数（网页「重新打分」按钮用）。"""
    resume = get_active_resume()
    if not resume:
        raise RuntimeError("没有生效的简历，请先上传简历")
    jobs = jobs_for_rescore()[:limit]
    logger.info("全量重算 {} 个岗位（简历 id={}）", len(jobs), resume.id)
    return _score_batch(jobs, resume)
