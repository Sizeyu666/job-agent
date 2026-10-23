"""APScheduler 定时任务（P6）：在 Web 进程内按 profile.schedule.cron 定时跑一轮流水线。

- cron 为 5 段「分 时 日 月 周」，按 Asia/Shanghai 解释；
- 流水线自带进程内任务锁，定时与手动同时触发时自动忽略后者，不会并发；
- 任何异常只写日志，不会让 Web 进程退出。
"""

from __future__ import annotations

from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from core.config import load_profile

_scheduler: Optional[BackgroundScheduler] = None

_FIELDS = ("minute", "hour", "day", "month", "day_of_week")


def parse_cron(expr: str) -> CronTrigger:
    """把「30 9 * * *」这类表达式转成 CronTrigger。"""
    parts = (expr or "").split()
    if len(parts) != 5:
        raise ValueError(f"cron 需要 5 段（分 时 日 月 周），当前是 {len(parts)} 段：{expr}")
    return CronTrigger(**dict(zip(_FIELDS, parts)), timezone="Asia/Shanghai")


def _run(trigger: str = "schedule") -> None:
    """定时触发入口：交给流水线（内部有锁，重入会直接返回）。"""
    try:
        from core.pipeline import run_once

        run_once(trigger=trigger)
    except Exception as e:  # noqa: BLE001 - 定时任务异常不能拖垮进程
        logger.exception("定时任务执行异常：{}", e)


def start_scheduler() -> dict:
    """按配置启动调度器（幂等），返回 {jobs, next}。"""
    global _scheduler
    if _scheduler and _scheduler.running:
        return {"jobs": len(_scheduler.get_jobs()), "next": get_next_run_text()}

    profile = load_profile()
    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    added = 0
    for expr in profile.schedule.cron:
        try:
            sched.add_job(
                _run,
                parse_cron(expr),
                id=f"cron:{expr}",
                kwargs={"trigger": "schedule"},
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=900,
            )
            added += 1
        except Exception as e:  # noqa: BLE001 - 单条表达式非法只跳过
            logger.warning("定时表达式无效，已跳过（{}）：{}", expr, e)

    sched.start()
    _scheduler = sched
    logger.info("定时任务已启动：{} 条计划，下次运行 {}", added, get_next_run_text())
    return {"jobs": added, "next": get_next_run_text()}


def get_next_run_text() -> Optional[str]:
    """下次运行时间（MM-DD HH:MM），未启动或无计划时返回 None。"""
    if not _scheduler or not _scheduler.running:
        return None
    try:
        times = [j.next_run_time for j in _scheduler.get_jobs() if getattr(j, "next_run_time", None)]
    except Exception as e:  # noqa: BLE001 - 调度器状态异常不应影响页面渲染
        logger.debug("读取下次运行时间失败：{}", e)
        return None
    return min(times).strftime("%m-%d %H:%M") if times else None


def shutdown_scheduler() -> None:
    """关闭调度器（Web 进程退出时调用）。"""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("定时任务已停止")
    _scheduler = None
