"""SQLModel 数据表：Job / Resume / Evaluation / Greeting / RunLog。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


class Job(SQLModel, table=True):
    """岗位（同一平台 + 平台岗位 ID 唯一）。"""

    __tablename__ = "job"
    __table_args__ = (UniqueConstraint("platform", "job_id", name="uq_job_platform_job_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(default="boss", index=True)
    job_id: str = Field(index=True, description="平台原始岗位 ID")
    url: str = ""
    title: str = ""
    company: str = ""
    salary_raw: str = ""
    city: str = ""
    experience: str = ""
    education: str = ""
    jd_text: str = ""
    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="pending", description="pending/applied/interviewed/ignored")
    first_seen_at: datetime = Field(default_factory=datetime.now)
    last_seen_at: datetime = Field(default_factory=datetime.now)
    is_deleted: bool = False


class Resume(SQLModel, table=True):
    """用户上传的简历（is_active 表示当前生效版本）。"""

    __tablename__ = "resume"

    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    file_type: str = ""
    raw_text: str = ""
    structured_json: str = Field(default="", description="LLM 输出的结构化 JSON 字符串")
    is_active: bool = False
    created_at: datetime = Field(default_factory=datetime.now)


class Evaluation(SQLModel, table=True):
    """某个岗位与某份简历的一次匹配评估结果。"""

    __tablename__ = "evaluation"
    __table_args__ = (
        UniqueConstraint("job_id", "resume_id", name="uq_evaluation_job_resume"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    resume_id: int = Field(foreign_key="resume.id", index=True)
    hard_passed: bool = True
    hard_gaps: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    embedding_score: float = 0.0
    skill_coverage: float = 0.0
    llm_score: float = 0.0
    final_score: float = 0.0
    matched_points: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    pitch_angle: str = ""
    risk_flags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    jd_summary: str = ""
    grade: str = Field(default="rejected", description="recommended/candidate/rejected")
    created_at: datetime = Field(default_factory=datetime.now)


class Greeting(SQLModel, table=True):
    """为某个岗位生成的打招呼话术。"""

    __tablename__ = "greeting"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    resume_id: int = Field(foreign_key="resume.id")
    style: str = Field(default="direct", description="direct/skill/sincere")
    content: str = ""
    created_at: datetime = Field(default_factory=datetime.now)


class RunLog(SQLModel, table=True):
    """每轮流水线运行记录。"""

    __tablename__ = "runlog"

    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None
    state: str = Field(default="running", description="running/success/failed/paused_captcha")
    new_jobs: int = 0
    evaluated: int = 0
    recommended: int = 0
    candidate: int = 0
    rejected: int = 0
    greeted_count: int = 0
    message: str = ""
    captcha_shot: str = ""
