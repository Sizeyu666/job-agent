"""主流水线：采集 → 打分 → 话术 → 推送。

run_once 加进程内任务锁，禁止并发；任何阶段的异常都会落到 RunLog，
验证码/登录失效时安全暂停并通知用户（绝不尝试绕过）。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Optional

from loguru import logger

from browser.session import BrowserSession, CaptchaError, LoginRequired, ensure_login
from core.config import Profile, load_profile
from core.db import (
    create_runlog,
    get_active_resume,
    get_runlog,
    latest_runlog,
    update_runlog,
    upsert_job,
)
from core.models import RunLog
from platforms.boss import BossAdapter

# 进程内任务锁：同一时间只允许一轮
_task_lock = threading.Lock()
_running = threading.Event()


def is_running() -> bool:
    """当前是否有流水线在运行。"""
    return _running.is_set()


def start_run_async(trigger: str = "manual") -> None:
    """在后台线程启动一轮流水线（Web「立即运行」按钮使用）。"""
    thread = threading.Thread(target=run_once, kwargs={"trigger": trigger}, daemon=True, name="jobagent-pipeline")
    thread.start()
    logger.info("后台流水线线程已启动（trigger={}）", trigger)


def _finish(run_id: int, state: str, message: str = "", **counters) -> RunLog:
    """更新运行记录并返回最新状态。"""
    fields = {
        "finished_at": datetime.now(),
        "state": state,
        "message": message,
        **counters,
    }
    update_runlog(run_id, **fields)
    run = get_runlog(run_id)
    logger.info("本轮结束：state={} message={} {}", state, message, counters)
    return run


def _notify(run: Optional[RunLog]) -> None:
    """推送汇总（推送模块未就绪或失败时只记录日志，不影响流水线）。"""
    if not run:
        return
    try:
        from notify.push import push_run_summary

        push_run_summary(run)
    except Exception as e:  # noqa: BLE001
        logger.warning("推送失败（不影响本轮结果）：{}", e)


def _collect_list(adapter: BossAdapter, profile: Profile) -> list[dict]:
    """遍历「关键词 × 城市」采集岗位列表，按 job_id 去重。"""
    items: list[dict] = []
    seen: set[str] = set()

    for keyword in profile.target_keywords:
        for city in profile.cities:
            try:
                found = adapter.search(keyword, city.code, profile.per_keyword_max_pages)
            except CaptchaError:
                raise  # 验证码必须向上抛出，暂停整轮
            except Exception as e:  # noqa: BLE001 - 单个关键词失败不影响其它关键词
                logger.warning("关键词「{}」·{} 采集失败：{}", keyword, city.name, e)
                continue

            added = 0
            for item in found:
                job_id = item.get("job_id")
                if not job_id or job_id in seen:
                    continue
                seen.add(job_id)
                items.append(item)
                added += 1
            logger.info("关键词「{}」·{}：新增 {} 个岗位（本轮累计 {}）", keyword, city.name, added, len(items))

            adapter._sleep()  # 关键词之间再停一下

    return items


def _fetch_details(adapter: BossAdapter, items: list[dict], limit: int) -> list[dict]:
    """为前 limit 个岗位抓取详情页 JD；单岗位失败隔离。"""
    limit = max(0, min(limit, len(items)))
    logger.info("开始抓取详情页，共 {} 个（上限 {}）", limit, limit)
    results: list[dict] = []
    for index, item in enumerate(items[:limit], start=1):
        try:
            detail = adapter.fetch_detail(adapter.page, item)
        except CaptchaError:
            raise
        except Exception as e:  # noqa: BLE001 - 单岗位失败不中断
            logger.warning("岗位详情抓取异常（{}）：{}", item.get("url"), e)
            detail = item
        results.append(detail)
        logger.info("详情页进度 {}/{}：{}", index, limit, detail.get("title"))
    return results


def _store_jobs(items: list[dict]) -> int:
    """岗位入库去重，返回新增数量。"""
    new_count = 0
    for item in items:
        try:
            _job, created = upsert_job(item)
            if created:
                new_count += 1
        except Exception as e:  # noqa: BLE001 - 单条入库失败隔离
            logger.warning("岗位入库失败（{}）：{}", item.get("job_id"), e)
    return new_count


def run_once(trigger: str = "schedule") -> Optional[RunLog]:
    """执行一轮完整流水线；并发调用时直接返回当前记录。"""
    if not _task_lock.acquire(blocking=False):
        logger.warning("已有任务在运行，本次触发被忽略（trigger={}）", trigger)
        return latest_runlog()

    _running.set()
    run = create_runlog()
    logger.info("====== 开始一轮任务（trigger={}，run_id={}）======", trigger, run.id)

    try:
        profile = load_profile()
        resume = get_active_resume()
        if not resume:
            failed_run = _finish(run.id, "failed", "未找到当前生效简历，请先在「简历管理」页上传简历")
            _notify(failed_run)
            return failed_run

        logger.info("使用简历：id={} 文件={}", resume.id, resume.filename)

        list_items: list[dict] = []
        new_jobs = 0

        if profile.collect_mode == "browser":
            with BrowserSession(headless=profile.browser_headless) as session:
                page = session.page
                ensure_login(page)

                adapter = BossAdapter(page, profile)
                list_items = _collect_list(adapter, profile)
                if not list_items:
                    failed_run = _finish(
                        run.id, "failed", "没有采集到任何岗位，请检查关键词/城市配置或 BOSS 页面结构"
                    )
                    _notify(failed_run)
                    return failed_run

                new_jobs = _store_jobs(list_items)

                detail_items = _fetch_details(adapter, list_items, profile.detail_page_limit)
                _store_jobs(detail_items)  # 已存在岗位会补全 JD，不重复计数
        else:
            logger.info("采集模式 manual：跳过浏览器采集，只对已入库岗位打分 / 生成话术")

        # ---- 打分（P3）----
        try:
            from matcher.skill import score_pending

            score_stats = score_pending(limit=max(50, profile.detail_page_limit))
        except Exception as e:  # noqa: BLE001 - 打分失败不影响已采集数据入库
            logger.exception("打分阶段失败：{}", e)
            score_stats = {"total": 0, "recommended": 0, "candidate": 0, "rejected": 0, "failed": 0}

        evaluated = score_stats.get("total", 0) - score_stats.get("failed", 0)
        recommended = score_stats.get("recommended", 0)
        candidate = score_stats.get("candidate", 0)
        rejected = score_stats.get("rejected", 0)

        # ---- 话术（P4）：只给推荐/可考虑的岗位生成 ----
        try:
            from core.greeting import generate_pending

            greet_stats = generate_pending(limit=max(10, profile.detail_page_limit))
            greeted = greet_stats.get("greeted", 0)
        except Exception as e:  # noqa: BLE001 - 话术失败不影响本轮打分结果
            logger.exception("话术生成阶段失败：{}", e)
            greeted = 0

        if profile.collect_mode == "browser":
            collect_msg = f"本轮采集 {len(list_items)} 个岗位，新增 {new_jobs} 个；"
        else:
            collect_msg = "手动采集模式（书签/粘贴），本轮不抓新岗位；"
        detail_msg = (
            collect_msg
            + f"打分 {evaluated} 个（推荐 {recommended}，可考虑 {candidate}，淘汰 {rejected}），"
            f"生成话术 {greeted} 个岗位"
        )

        success_run = _finish(
            run.id,
            "success",
            detail_msg,
            new_jobs=new_jobs,
            evaluated=evaluated,
            recommended=recommended,
            candidate=candidate,
            rejected=rejected,
            greeted_count=greeted,
        )
        _notify(success_run)
        return success_run

    except CaptchaError as e:
        logger.warning("遇到验证码，暂停本轮：{}", e)
        paused = _finish(
            run.id,
            "paused_captcha",
            "检测到安全验证/滑块，本轮已暂停。请在浏览器中手动完成验证后重新运行。",
        )
        update_runlog(run.id, captcha_shot=getattr(e, "screenshot", ""))
        _notify(get_runlog(run.id))
        return paused

    except LoginRequired as e:
        logger.warning("登录态失效：{}", e)
        failed_run = _finish(
            run.id, "failed", f"{e}。请先运行 python main.py login 完成扫码登录。"
        )
        _notify(failed_run)
        return failed_run

    except Exception as e:  # noqa: BLE001 - 任何异常都要落到运行记录，且不崩溃 Web 进程
        logger.exception("流水线异常终止：{}", e)
        failed_run = _finish(run.id, "failed", f"运行异常：{e}")
        _notify(failed_run)
        return failed_run

    finally:
        _running.clear()
        _task_lock.release()
        logger.info("====== 本轮任务结束（run_id={}）======", run.id)
