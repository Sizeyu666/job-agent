"""数据库 CRUD 与去重逻辑（SQLite + SQLModel）。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Optional

from loguru import logger
from sqlmodel import Session, create_engine, delete, func, select

from core.config import DATA_DIR
from core.models import Evaluation, Greeting, Job, Resume, RunLog, SQLModel

DB_PATH = DATA_DIR / "job_agent.db"
_engine = None


def get_engine():
    """获取（懒加载）SQLite 引擎，允许跨线程使用。"""
    global _engine
    if _engine is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{DB_PATH.as_posix()}",
            connect_args={"check_same_thread": False},
        )
        logger.debug("数据库引擎已创建：{}", DB_PATH)
    return _engine


def init_db() -> None:
    """建表（幂等）。"""
    SQLModel.metadata.create_all(get_engine())
    logger.info("数据库已初始化：{}", DB_PATH)


@contextmanager
def get_session() -> Iterator[Session]:
    """会话上下文：正常结束自动 commit，异常 rollback。"""
    session = Session(get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ------------------------- Job -------------------------
def upsert_job(data: dict) -> tuple[Job, bool]:
    """按 (platform, job_id) 去重写入岗位；已存在则刷新 last_seen_at 并补全空字段。

    返回 (岗位对象, 是否新岗位)。
    """
    platform = data.get("platform", "boss")
    job_id = str(data.get("job_id", "")).strip()
    if not job_id:
        raise ValueError("岗位缺少 job_id，无法去重入库")

    with get_session() as s:
        existing = s.exec(
            select(Job).where(Job.platform == platform, Job.job_id == job_id)
        ).first()
        if existing:
            existing.last_seen_at = datetime.now()
            # 仅补全空字段，避免覆盖已抓到的更完整信息
            for key in ("url", "title", "company", "salary_raw", "city",
                        "experience", "education", "jd_text"):
                if not getattr(existing, key) and data.get(key):
                    setattr(existing, key, data[key])
            if data.get("tags") and not existing.tags:
                existing.tags = list(data["tags"])
            s.add(existing)
            return existing, False

        job = Job(
            platform=platform,
            job_id=job_id,
            url=data.get("url", ""),
            title=data.get("title", ""),
            company=data.get("company", ""),
            salary_raw=data.get("salary_raw", ""),
            city=data.get("city", ""),
            experience=data.get("experience", ""),
            education=data.get("education", ""),
            jd_text=data.get("jd_text", ""),
            tags=list(data.get("tags") or []),
        )
        s.add(job)
        s.flush()
        s.refresh(job)
        return job, True


def get_job(job_pk: int) -> Optional[Job]:
    """按主键取岗位。"""
    with get_session() as s:
        return s.get(Job, job_pk)


def set_job_status(job_pk: int, status: str) -> Optional[Job]:
    """更新岗位处理状态（待处理/已投递/已约面/不感兴趣）。"""
    allowed = {"pending", "applied", "interviewed", "ignored"}
    if status not in allowed:
        raise ValueError(f"非法状态：{status}")
    with get_session() as s:
        job = s.get(Job, job_pk)
        if job:
            job.status = status
            s.add(job)
        return job


def list_jobs(
    grade: Optional[str] = None,
    city: Optional[str] = None,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    score_min: Optional[float] = None,
    score_max: Optional[float] = None,
    limit: int = 500,
) -> list[dict]:
    """按分档/城市/状态/关键词/分数区间查询岗位，按最终分降序返回 dict 列表。"""
    with get_session() as s:
        stmt = (
            select(Job, Evaluation)
            .join(Evaluation, Evaluation.job_id == Job.id, isouter=True)
            .where(Job.is_deleted == False)  # noqa: E712
        )
        if grade:
            stmt = stmt.where(Evaluation.grade == grade)
        if city:
            stmt = stmt.where(Job.city.contains(city))
        if status:
            stmt = stmt.where(Job.status == status)
        if keyword:
            like = f"%{keyword}%"
            stmt = stmt.where(
                (Job.title.like(like)) | (Job.company.like(like)) | (Job.jd_text.like(like))
            )
        if score_min is not None:
            stmt = stmt.where(Evaluation.final_score >= score_min)
        if score_max is not None:
            stmt = stmt.where(Evaluation.final_score <= score_max)
        stmt = stmt.order_by(Evaluation.final_score.desc().nulls_last(), Job.id.desc()).limit(limit)
        rows = s.exec(stmt).all()
        return [_job_row_to_dict(job, ev) for job, ev in rows]


def _job_row_to_dict(job: Job, ev: Optional[Evaluation]) -> dict:
    """把 Job + Evaluation 组合成模板可直接使用的 dict。"""
    return {
        "job": job,
        "evaluation": ev,
        "final_score": round(ev.final_score, 1) if ev else None,
        "grade": ev.grade if ev else "unrated",
        "matched_points": (ev.matched_points or [])[:3] if ev else [],
        "risk_flags": ev.risk_flags if ev else [],
    }


def delete_job(job_pk: int) -> None:
    """软删除岗位。"""
    with get_session() as s:
        job = s.get(Job, job_pk)
        if job:
            job.is_deleted = True
            s.add(job)


def jobs_for_rescore() -> list[Job]:
    """需要重新打分的岗位：全部未删除岗位。"""
    with get_session() as s:
        return list(s.exec(select(Job).where(Job.is_deleted == False)).all())  # noqa: E712


# ------------------------- Resume -------------------------
def create_resume(
    filename: str, file_type: str, raw_text: str, structured_json: str = "", active: bool = True
) -> Resume:
    """新建简历记录；active=True 时把其它简历置为非激活。"""
    with get_session() as s:
        if active:
            for item in s.exec(select(Resume).where(Resume.is_active == True)).all():  # noqa: E712
                item.is_active = False
                s.add(item)
        resume = Resume(
            filename=filename,
            file_type=file_type,
            raw_text=raw_text,
            structured_json=structured_json,
            is_active=active,
        )
        s.add(resume)
        s.flush()
        s.refresh(resume)
        return resume


def update_resume(resume_id: int, raw_text: Optional[str] = None, structured_json: Optional[str] = None) -> Optional[Resume]:
    """更新简历的解析文本或结构化结果。"""
    with get_session() as s:
        resume = s.get(Resume, resume_id)
        if not resume:
            return None
        if raw_text is not None:
            resume.raw_text = raw_text
        if structured_json is not None:
            resume.structured_json = structured_json
        s.add(resume)
        return resume


def set_active_resume(resume_id: int) -> Optional[Resume]:
    """切换当前生效简历。"""
    with get_session() as s:
        target = s.get(Resume, resume_id)
        if not target:
            return None
        for item in s.exec(select(Resume)).all():
            item.is_active = item.id == resume_id
            s.add(item)
        return target


def get_active_resume() -> Optional[Resume]:
    """取当前生效简历。"""
    with get_session() as s:
        return s.exec(
            select(Resume).where(Resume.is_active == True).order_by(Resume.id.desc())  # noqa: E712
        ).first()


def list_resumes() -> list[Resume]:
    """按上传时间倒序列出全部简历。"""
    with get_session() as s:
        return list(s.exec(select(Resume).order_by(Resume.created_at.desc())).all())


# ------------------------- Evaluation -------------------------
def get_evaluation(job_pk: int, resume_id: int) -> Optional[Evaluation]:
    """取某岗位 + 某简历的评估结果。"""
    with get_session() as s:
        return s.exec(
            select(Evaluation).where(
                Evaluation.job_id == job_pk, Evaluation.resume_id == resume_id
            )
        ).first()


def upsert_evaluation(job_pk: int, resume_id: int, data: dict) -> Evaluation:
    """写入/更新评估结果（job_id + resume_id 唯一）。"""
    with get_session() as s:
        ev = s.exec(
            select(Evaluation).where(
                Evaluation.job_id == job_pk, Evaluation.resume_id == resume_id
            )
        ).first()
        if not ev:
            ev = Evaluation(job_id=job_pk, resume_id=resume_id)
        for key, value in data.items():
            setattr(ev, key, value)
        ev.created_at = datetime.now()
        s.add(ev)
        s.flush()
        s.refresh(ev)
        return ev


def jobs_without_evaluation(resume_id: int, limit: int = 200) -> list[Job]:
    """取尚未用该简历打分的岗位。"""
    with get_session() as s:
        sub = select(Evaluation.job_id).where(Evaluation.resume_id == resume_id)
        stmt = (
            select(Job)
            .where(Job.is_deleted == False, Job.id.not_in(sub))  # noqa: E712
            .order_by(Job.id.asc())
            .limit(limit)
        )
        return list(s.exec(stmt).all())


def jobs_needing_greeting(resume_id: int, limit: int = 100) -> list[tuple[Job, Evaluation]]:
    """取判定为推荐/可考虑、但还没有话术的岗位。"""
    with get_session() as s:
        sub = select(Greeting.job_id)
        stmt = (
            select(Job, Evaluation)
            .join(Evaluation, Evaluation.job_id == Job.id)
            .where(
                Evaluation.resume_id == resume_id,
                Evaluation.grade.in_(["recommended", "candidate"]),
                Job.id.not_in(sub),
            )
            .order_by(Evaluation.final_score.desc())
            .limit(limit)
        )
        return list(s.exec(stmt).all())


# ------------------------- Greeting -------------------------
def replace_greetings(job_pk: int, resume_id: int, items: Iterable[dict]) -> int:
    """替换某岗位的全部话术（先删后插），返回写入条数。"""
    items = list(items)
    with get_session() as s:
        s.exec(delete(Greeting).where(Greeting.job_id == job_pk))
        for item in items:
            s.add(
                Greeting(
                    job_id=job_pk,
                    resume_id=resume_id,
                    style=item.get("style", "direct"),
                    content=item.get("content", ""),
                )
            )
        s.flush()
    return len(items)


def get_greetings(job_pk: int) -> list[Greeting]:
    """取某岗位的话术列表。"""
    with get_session() as s:
        return list(
            s.exec(select(Greeting).where(Greeting.job_id == job_pk).order_by(Greeting.id))
        )


def greetings_map(job_ids: list[int]) -> dict[int, list[Greeting]]:
    """批量取话术，返回 {job_id: [Greeting]}。"""
    if not job_ids:
        return {}
    with get_session() as s:
        rows = s.exec(select(Greeting).where(Greeting.job_id.in_(job_ids))).all()
        result: dict[int, list[Greeting]] = {}
        for g in rows:
            result.setdefault(g.job_id, []).append(g)
        return result


def delete_greetings() -> None:
    """清空全部话术（重新生成时使用）。"""
    with get_session() as s:
        s.exec(delete(Greeting))


# ------------------------- RunLog -------------------------
def create_runlog() -> RunLog:
    """新建一条 running 状态的运行记录。"""
    with get_session() as s:
        run = RunLog(state="running")
        s.add(run)
        s.flush()
        s.refresh(run)
        return run


def update_runlog(run_id: int, **fields) -> Optional[RunLog]:
    """更新运行记录字段。"""
    with get_session() as s:
        run = s.get(RunLog, run_id)
        if not run:
            return None
        for key, value in fields.items():
            if hasattr(run, key):
                setattr(run, key, value)
        s.add(run)
        return run


def get_runlog(run_id: int) -> Optional[RunLog]:
    """按主键取运行记录。"""
    with get_session() as s:
        return s.get(RunLog, run_id)


def latest_runlog() -> Optional[RunLog]:
    """最近一条运行记录。"""
    with get_session() as s:
        return s.exec(select(RunLog).order_by(RunLog.id.desc())).first()


def list_runlogs(limit: int = 100) -> list[RunLog]:
    """运行记录列表（最新在前）。"""
    with get_session() as s:
        return list(s.exec(select(RunLog).order_by(RunLog.id.desc()).limit(limit)).all())


# ------------------------- 统计 -------------------------
def dashboard_stats() -> dict:
    """仪表盘统计：今日新增/推荐/可考虑/已投递/各分档总数。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with get_session() as s:
        today_new = s.exec(
            select(func.count(Job.id)).where(Job.first_seen_at >= today, Job.is_deleted == False)  # noqa: E712
        ).one()
        recommended = s.exec(
            select(func.count(Evaluation.id)).where(Evaluation.grade == "recommended")
        ).one()
        candidate = s.exec(
            select(func.count(Evaluation.id)).where(Evaluation.grade == "candidate")
        ).one()
        rejected = s.exec(
            select(func.count(Evaluation.id)).where(Evaluation.grade == "rejected")
        ).one()
        applied = s.exec(
            select(func.count(Job.id)).where(Job.status.in_(["applied", "interviewed"]))
        ).one()
        total = s.exec(select(func.count(Job.id)).where(Job.is_deleted == False)).one()  # noqa: E712
        buckets = {
            "90-100": s.exec(
                select(func.count(Evaluation.id)).where(Evaluation.final_score >= 90)
            ).one(),
            "75-89": s.exec(
                select(func.count(Evaluation.id)).where(
                    Evaluation.final_score >= 75, Evaluation.final_score < 90
                )
            ).one(),
            "50-74": s.exec(
                select(func.count(Evaluation.id)).where(
                    Evaluation.final_score >= 50, Evaluation.final_score < 75
                )
            ).one(),
            "0-49": s.exec(
                select(func.count(Evaluation.id)).where(Evaluation.final_score < 50)
            ).one(),
        }
    return {
        "today_new": today_new or 0,
        "total_jobs": total or 0,
        "recommended": recommended or 0,
        "candidate": candidate or 0,
        "rejected": rejected or 0,
        "applied": applied or 0,
        "buckets": buckets,
    }
