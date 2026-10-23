"""FastAPI 应用：Web 平台路由（Jinja2 模板 + Tailwind CDN + Alpine.js）。"""

from __future__ import annotations

import hashlib
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from pydantic import BaseModel, Field

from core.config import load_profile
from core.db import (
    dashboard_stats,
    get_active_resume,
    get_evaluation,
    get_greetings,
    get_job,
    init_db,
    latest_runlog,
    list_jobs,
    list_resumes,
    list_runlogs,
    set_active_resume,
    set_job_status,
)

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _fmt_dt(value, fmt: str = "%m-%d %H:%M") -> str:
    """模板过滤器：格式化时间。"""
    if not value:
        return "—"
    try:
        return value.strftime(fmt)
    except Exception:  # noqa: BLE001 - 时间格式异常不应影响页面渲染
        return str(value)


templates.env.filters["dt"] = _fmt_dt


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化数据库并拉起定时任务；关闭时停止调度器。"""
    init_db()
    logger.info("JobAgent Web 服务启动")
    try:
        from core.scheduler import start_scheduler

        info = start_scheduler()
        logger.info("定时任务就绪：{} 条计划，下次运行 {}", info.get("jobs"), info.get("next"))
    except Exception as e:  # noqa: BLE001 - 调度器异常不影响网页可用
        logger.warning("启动定时任务失败：{}", e)
    yield
    try:
        from core.scheduler import shutdown_scheduler

        shutdown_scheduler()
    except Exception as e:  # noqa: BLE001 - 关闭阶段异常仅记录
        logger.warning("停止调度器失败：{}", e)


app = FastAPI(title="JobAgent", lifespan=lifespan)
# 书签脚本从 www.zhipin.com 页面跨域推到本机 127.0.0.1，需要放开 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def allow_private_network(request: Request, call_next):
    """Chrome 私有网络访问（公网页 → 127.0.0.1）预检需要显式放行。"""
    response = await call_next(request)
    if request.method == "OPTIONS" and request.headers.get("access-control-request-private-network"):
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")


class IngestJob(BaseModel):
    """书签推送的单条岗位。"""

    job_id: str = ""
    title: str = ""
    company: str = ""
    salary: str = ""
    city: str = ""
    experience: str = ""
    education: str = ""
    tags: list[str] = Field(default_factory=list)
    url: str = ""
    jd_text: str = ""


class IngestPayload(BaseModel):
    """书签推送的一页岗位。"""

    keyword: str = ""
    platform: str = "boss"
    jobs: list[IngestJob] = Field(default_factory=list)


class ManualPayload(BaseModel):
    """手动粘贴的岗位页面文字。"""

    text: str = ""
    url: str = ""


def render(request: Request, template: str, ctx: dict | None = None) -> HTMLResponse:
    """渲染模板，自动注入通用上下文。"""
    profile = load_profile()
    context = {"request": request, "profile": profile, "active": "", "app_name": "JobAgent"}
    context.update(ctx or {})
    return templates.TemplateResponse(template, context)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """仪表盘：今日采集/推荐统计、任务状态、分数分布。"""
    stats = dashboard_stats()
    run = latest_runlog()
    next_run_text = None
    try:
        from core.scheduler import get_next_run_text

        next_run_text = get_next_run_text()
    except Exception as e:  # noqa: BLE001 - 调度器未启动时正常降级
        logger.debug("读取下次运行时间失败：{}", e)
    return render(
        request,
        "dashboard.html",
        {"active": "dashboard", "stats": stats, "run": run, "next_run_text": next_run_text},
    )


@app.post("/api/run")
def api_run() -> dict:
    """手动触发一轮流水线（后台线程执行，禁止并发）。"""
    try:
        from core.pipeline import is_running, start_run_async
    except Exception as e:  # noqa: BLE001 - 流水线模块异常时给出明确提示
        logger.error("流水线不可用：{}", e)
        return {"ok": False, "message": f"流水线暂不可用：{e}"}
    if is_running():
        return {"ok": False, "message": "已有一轮任务正在运行，请稍候"}
    start_run_async(trigger="manual")
    return {"ok": True, "message": "已启动"}


@app.get("/api/run/status")
def api_run_status() -> dict:
    """查询当前是否有流水线在运行（前端轮询用）。"""
    try:
        from core.pipeline import is_running

        return {"running": is_running()}
    except Exception:  # noqa: BLE001
        return {"running": False}


@app.post("/api/ingest")
def api_ingest(payload: IngestPayload) -> dict:
    """浏览器书签推送的岗位入库（按 platform+job_id 去重），支持 BOSS 与实习僧。"""
    from core.db import upsert_job

    platform = payload.platform if payload.platform in ("boss", "shixiseng") else "boss"
    id_patterns = {
        "boss": r"/job_detail/([^.]+?)\.html",
        "shixiseng": r"/intern/(inn_[0-9a-zA-Z]+)",
    }
    added = updated = skipped = 0
    for item in payload.jobs:
        job_id = (item.job_id or "").strip()
        if not job_id:
            matched = re.search(id_patterns[platform], item.url or "")
            job_id = matched.group(1) if matched else ""
        if not job_id:
            skipped += 1
            continue
        if not (item.title or "").strip() and not (item.jd_text or "").strip():
            # 详情页没抓到（岗位已下架等），只剩公司名没法打分，直接跳过
            skipped += 1
            continue

        record = {
            "platform": platform,
            "job_id": job_id,
            "url": item.url,
            "title": item.title,
            "company": item.company,
            "salary_raw": item.salary,
            "city": item.city,
            "experience": item.experience,
            "education": item.education,
            "tags": item.tags,
            "jd_text": item.jd_text,
        }
        record = {k: v for k, v in record.items() if v not in ("", None, [])}
        record.update({"platform": platform, "job_id": job_id})
        try:
            _job, created = upsert_job(record)
            if created:
                added += 1
            else:
                updated += 1
        except Exception as e:  # noqa: BLE001 - 单条失败不影响整页
            logger.warning("书签数据入库失败（{}）：{}", job_id, e)
            skipped += 1

    logger.info("书签采集入库：新增 {}，更新 {}，跳过 {}（关键词 {}）", added, updated, skipped, payload.keyword)
    return {"ok": True, "added": added, "updated": updated, "skipped": skipped}


@app.post("/api/ingest/manual")
def api_ingest_manual(payload: ManualPayload) -> dict:
    """手动粘贴岗位文字 → LLM 结构化 → 入库（BOSS 打不开时的备用通道）。"""
    from core.db import upsert_job
    from core.llm import chat_json
    from core.prompts import JOB_PARSE_SYSTEM, JOB_PARSE_USER

    text = (payload.text or "").strip()
    if len(text) < 30:
        return {"ok": False, "message": "粘贴内容太短：请把岗位详情页正文整体复制过来"}

    try:
        parsed = chat_json(JOB_PARSE_SYSTEM, JOB_PARSE_USER.format(job_text=text[:8000]))
    except Exception as e:  # noqa: BLE001 - 解析失败原样告知用户
        logger.warning("手动粘贴解析失败：{}", e)
        return {"ok": False, "message": f"解析失败：{e}"}

    title = str(parsed.get("title") or "").strip()
    company = str(parsed.get("company") or "").strip()
    if not title:
        return {"ok": False, "message": "没能识别出岗位名称，请把职位详情页正文完整复制后再试"}

    url = (payload.url or "").strip()
    platform = "shixiseng" if "shixiseng.com" in url else "boss"
    pattern = r"/intern/(inn_[0-9a-zA-Z]+)" if platform == "shixiseng" else r"/job_detail/([^.]+?)\.html"
    matched = re.search(pattern, url)
    job_id = matched.group(1) if matched else "manual_" + hashlib.md5(f"{company}|{title}".encode()).hexdigest()[:12]

    tags = parsed.get("tags") or []
    record = {
        "platform": platform,
        "job_id": job_id,
        "url": url,
        "title": title,
        "company": company,
        "salary_raw": str(parsed.get("salary") or "").strip(),
        "city": str(parsed.get("city") or "").strip(),
        "experience": str(parsed.get("experience") or "").strip(),
        "education": str(parsed.get("education") or "").strip(),
        "tags": [str(t).strip() for t in tags if str(t).strip()][:12],
        "jd_text": str(parsed.get("jd_text") or "").strip(),
    }
    try:
        _job, created = upsert_job(record)
    except Exception as e:  # noqa: BLE001
        logger.warning("手动粘贴入库失败：{}", e)
        return {"ok": False, "message": f"入库失败：{e}"}

    logger.info("手动粘贴入库：{} @ {}（{}）", title, company, "新增" if created else "更新")
    return {
        "ok": True,
        "created": created,
        "job": {
            "title": title,
            "company": company,
            "salary": record["salary_raw"],
            "city": record["city"],
            "experience": record["experience"],
            "education": record["education"],
            "tags": record["tags"],
            "jd_chars": len(record["jd_text"]),
        },
    }


@app.post("/api/score")
def api_score(limit: int = 100) -> dict:
    """给尚未打分的岗位打分（书签采集后调用）。"""
    try:
        from matcher.skill import score_pending

        stats = score_pending(limit=limit)
        return {"ok": True, **stats}
    except Exception as e:  # noqa: BLE001
        logger.exception("打分失败：{}", e)
        return {"ok": False, "message": str(e)}


@app.get("/bookmarklet", response_class=HTMLResponse)
def bookmarklet_page(request: Request) -> HTMLResponse:
    """一键采集页：给出可拖到收藏夹的书签脚本与使用说明。"""
    port = load_profile().web.port
    return render(
        request,
        "bookmarklet.html",
        {"active": "bookmarklet", "port": port, "bookmarklet": _bookmarklet_js(port)},
    )


def _bookmarklet_js(port: int) -> str:
    """生成书签脚本：同时支持 BOSS 直聘与实习僧，用当前浏览器登录态读页面后推给本机接口。

    实习僧列表页把标题/薪资换成了图标字体（DOM 文本是私用区乱码），
    因此该站改为「列表取 id 与链接 → 逐条 fetch 详情页（详情页文本是明文）」。
    """
    js = r"""
(function(){
  var host=location.hostname;
  var site=/zhipin\.com$/.test(host)?'boss':(/shixiseng\.com$/.test(host)?'shixiseng':'');
  if(!site){alert('这个书签只在 BOSS 直聘或实习僧的页面上使用。');return;}
  (async function(){
    try{
      var jobs=[];
      var limit=8;
      if(site==='boss'){
        var cards=Array.prototype.slice.call(document.querySelectorAll('.job-card-wrapper'));
        if(!cards.length){alert('没找到岗位卡片：请确认当前是 BOSS 职位搜索页，并已加载出岗位列表。');return;}
        jobs=cards.map(function(c){
          var a=c.querySelector('a[href*="/job_detail/"]');
          var url=a?a.href:'';
          var m=url.match(/job_detail\/([^.]+)\.html/);
          var tags=Array.prototype.slice.call(c.querySelectorAll('.tag-list li')).map(function(x){return (x.innerText||'').trim();}).filter(Boolean);
          var pick=function(sel){var e=c.querySelector(sel);return e?(e.innerText||'').trim():'';};
          return {job_id:m?m[1]:'',url:url,title:pick('.job-name'),company:pick('.company-name'),
                  salary:pick('.salary'),city:pick('.job-area'),
                  experience:tags[0]||'',education:tags[1]||'',tags:tags,jd_text:''};
        });
        limit=Math.min(limit,jobs.length);
        for(var i=0;i<limit;i++){
          try{
            var r=await fetch('/wapi/zpgeek/job/detail.json?encryptJobId='+encodeURIComponent(jobs[i].job_id),{credentials:'include'});
            var d=await r.json();
            var info=(d&&d.zpData&&(d.zpData.jobInfo||d.zpData))||{};
            var desc=info.postDescription||info.jobDescription||info.description||'';
            jobs[i].jd_text=String(desc).replace(/<[^>]+>/g,'\n').replace(/\n{3,}/g,'\n\n').trim().slice(0,4000);
          }catch(e){}
          await new Promise(function(res){setTimeout(res,700);});
        }
      }else{
        var cards=Array.prototype.slice.call(document.querySelectorAll('.intern-item'));
        if(!cards.length){alert('没找到实习岗位卡片：请确认当前是实习僧搜索结果页，并已加载出列表。');return;}
        jobs=cards.map(function(c){
          var a=c.querySelector('a[href*="/intern/"]');
          var url=a?a.href.split('?')[0]:'';
          var m=url.match(/intern\/(inn_[0-9a-zA-Z]+)/);
          var cid=c.getAttribute('data-intern-id')||'';
          var pick=function(sel){var e=c.querySelector(sel);return e?(e.innerText||'').trim():'';};
          return {job_id:cid||(m?m[1]:''),url:url,title:'',company:pick('.intern-detail__company a.title'),
                  salary:'',city:pick('.city'),experience:'',education:'',tags:[],jd_text:''};
        });
        limit=Math.min(20,jobs.length);
        for(var i=0;i<limit;i++){
          try{
            if(!jobs[i].url){continue;}
            var r=await fetch(jobs[i].url,{credentials:'include'});
            var html=await r.text();
            var doc=new DOMParser().parseFromString(html,'text/html');
            var pick2=function(sel){var e=doc.querySelector(sel);return e?(e.textContent||'').trim():'';};
            jobs[i].title=pick2('.new_job_name');
            jobs[i].salary=pick2('.job_money');
            jobs[i].city=pick2('.job_position')||jobs[i].city;
            jobs[i].education=pick2('.job_academic');
            var week=pick2('.job_week');
            var mon=pick2('.job_time');
            jobs[i].experience=[week,mon].filter(Boolean).join(' ');
            jobs[i].tags=Array.prototype.slice.call(doc.querySelectorAll('.job_good_list span')).map(function(x){return (x.textContent||'').trim();}).filter(Boolean);
            jobs[i].jd_text=(pick2('.job_detail')||'').replace(/\n{3,}/g,'\n\n').slice(0,4000);
          }catch(e){}
          await new Promise(function(res){setTimeout(res,800);});
        }
      }
      var qs=new URLSearchParams(location.search);
      var res=await fetch('http://127.0.0.1:PORMARK/api/ingest',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({platform:site,keyword:qs.get('query')||qs.get('keyword')||'',jobs:jobs})});
      var out=await res.json();
      alert('已采集 '+jobs.length+' 个岗位（'+(site==='boss'?'BOSS':'实习僧')+'）：新增 '+out.added+'，更新 '+out.updated+'，跳过 '+out.skipped+'。\n其中 '+limit+' 个带 JD，回 JobAgent 点「立即打分」。');
    }catch(e){alert('采集失败：'+e.message+'\n请确认 JobAgent 服务已启动（http://127.0.0.1:PORMARK）。');}
  })();
})();
""".replace("PORMARK", str(port))
    return "javascript:" + " ".join(line.strip() for line in js.strip().splitlines())


@app.get("/resume", response_class=HTMLResponse)
def resume_page(request: Request, msg: Optional[str] = None, err: Optional[str] = None) -> HTMLResponse:
    """简历管理：当前生效简历、上传表单、历史版本。"""
    from core.resume import parse_structured

    active = get_active_resume()
    structured = parse_structured(active) if active else {}
    return render(
        request,
        "resume.html",
        {
            "active": "resume",
            "resume": active,
            "structured": structured,
            "resumes": list_resumes(),
            "msg": msg,
            "err": err,
        },
    )


@app.post("/resume/upload")
def resume_upload(file: UploadFile = File(...)) -> RedirectResponse:
    """上传简历：保存文件 → 解析文本 → LLM 结构化 → 设为当前生效。"""
    from core.resume import ingest_resume_file, save_upload_bytes

    try:
        data = file.file.read()
        if not data:
            raise ValueError("上传文件为空")
        path = save_upload_bytes(file.filename or "resume", data)
        resume = ingest_resume_file(path, original_name=file.filename)
    except Exception as e:  # noqa: BLE001 - 上传失败要把原因反馈到页面
        logger.error("简历上传失败：{}", e)
        return RedirectResponse(f"/resume?err={str(e)}", status_code=303)
    return RedirectResponse(f"/resume?msg=简历已上传并解析（id={resume.id}）", status_code=303)


@app.post("/resume/{resume_id}/activate")
def resume_activate(resume_id: int) -> RedirectResponse:
    """把指定简历切换为当前生效版本。"""
    if not set_active_resume(resume_id):
        return RedirectResponse("/resume?err=简历不存在", status_code=303)
    return RedirectResponse(f"/resume?msg=已切换到简历 #{resume_id}", status_code=303)


# ------------------------- P5：岗位列表 / 详情 / 设置 / 运行日志 -------------------------
STATUS_LABELS = {
    "pending": "待处理",
    "applied": "已投递",
    "interviewed": "已约面",
    "ignored": "不感兴趣",
}
STYLE_LABELS = {"direct": "项目直击型", "skill": "技能匹配型", "sincere": "诚意请教型"}


class StatusPayload(BaseModel):
    """岗位状态更新请求。"""

    status: str = ""


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(
    request: Request,
    grade: Optional[str] = None,
    city: Optional[str] = None,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    score_min: Optional[float] = None,
    score_max: Optional[float] = None,
) -> HTMLResponse:
    """岗位列表：分档 Tab + 筛选，按最终分降序。"""
    from core.db import greetings_map

    rows = list_jobs(
        grade=grade or None,
        city=city or None,
        status=status or None,
        keyword=keyword or None,
        score_min=score_min,
        score_max=score_max,
        limit=500,
    )
    return render(
        request,
        "jobs.html",
        {
            "active": "jobs",
            "rows": rows,
            "gmap": greetings_map([r["job"].id for r in rows]),
            "stats": dashboard_stats(),
            "status_labels": STATUS_LABELS,
            "style_labels": STYLE_LABELS,
            "filters": {
                "grade": grade or "",
                "city": city or "",
                "status": status or "",
                "keyword": keyword or "",
                "score_min": score_min,
                "score_max": score_max,
            },
        },
    )


@app.get("/jobs/{job_pk}", response_class=HTMLResponse)
def job_detail_page(request: Request, job_pk: int, msg: Optional[str] = None) -> HTMLResponse:
    """岗位详情：JD 全文、分数拆解、匹配点、风险、话术。"""
    job = get_job(job_pk)
    if not job:
        return render(request, "job_detail.html", {"active": "jobs", "job": None, "msg": msg})

    resume = get_active_resume()
    evaluation = get_evaluation(job_pk, resume.id) if resume else None
    return render(
        request,
        "job_detail.html",
        {
            "active": "jobs",
            "job": job,
            "evaluation": evaluation,
            "greetings": get_greetings(job_pk),
            "status_labels": STATUS_LABELS,
            "style_labels": STYLE_LABELS,
            "msg": msg,
        },
    )


@app.post("/jobs/{job_pk}/status")
def api_job_status(job_pk: int, payload: StatusPayload) -> dict:
    """AJAX 更新岗位状态。"""
    try:
        job = set_job_status(job_pk, (payload.status or "").strip())
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    if not job:
        return {"ok": False, "message": "岗位不存在"}
    return {"ok": True, "status": job.status, "label": STATUS_LABELS.get(job.status, job.status)}


@app.post("/jobs/{job_pk}/greeting")
def api_job_greeting(job_pk: int) -> dict:
    """为单个岗位重新生成话术。"""
    from core.greeting import generate_for_job

    try:
        result = generate_for_job(job_pk)
        return {"ok": True, **result}
    except Exception as e:  # noqa: BLE001 - 生成失败要把原因给页面
        logger.warning("单岗位话术生成失败（{}）：{}", job_pk, e)
        return {"ok": False, "message": str(e)}


@app.post("/api/rescore")
def api_rescore() -> dict:
    """后台线程用当前简历重算全部岗位分数。"""
    import threading

    def _work() -> None:
        try:
            from matcher.skill import rescore_all

            stats = rescore_all()
            logger.info("重新打分完成：{}", stats)
        except Exception as e:  # noqa: BLE001 - 后台任务异常只写日志
            logger.exception("重新打分失败：{}", e)

    threading.Thread(target=_work, daemon=True, name="jobagent-rescore").start()
    return {"ok": True, "message": "已开始用当前简历重新打分，稍后刷新查看"}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request, msg: Optional[str] = None, err: Optional[str] = None
) -> HTMLResponse:
    """设置页：profile.yaml 表单化 + secrets 密钥。"""
    from core.config import load_secrets

    return render(
        request,
        "settings.html",
        {"active": "settings", "secrets": load_secrets(), "msg": msg, "err": err},
    )


def _split_lines(text: str) -> list[str]:
    """按换行/逗号/分号拆分多值输入，去空。"""
    parts = re.split(r"[\n,，;；]+", text or "")
    return [p.strip() for p in parts if p.strip()]


def _parse_cities(text: str) -> list[dict]:
    """解析「南京 101190100」这类城市行。"""
    cities: list[dict] = []
    for line in _split_lines(text):
        parts = re.split(r"[|\s]+", line.strip())
        if len(parts) >= 2 and parts[1].isdigit():
            cities.append({"name": parts[0], "code": parts[1]})
    return cities


@app.post("/settings")
def settings_save(
    keywords: str = Form(""),
    cities: str = Form(""),
    score_threshold: int = Form(75),
    per_keyword_max_pages: int = Form(2),
    detail_page_limit: int = Form(60),
    min_delay_seconds: int = Form(3),
    max_delay_seconds: int = Form(8),
    collect_mode: str = Form("manual"),
    notify_feishu: Optional[str] = Form(None),
    notify_serverchan: Optional[str] = Form(None),
    cron: str = Form(""),
    llm_base_url: str = Form(""),
    llm_model: str = Form(""),
    hard_filter_enabled: Optional[str] = Form(None),
    api_key: str = Form(""),
    feishu_webhook: str = Form(""),
    serverchan_sendkey: str = Form(""),
) -> RedirectResponse:
    """保存设置：先校验 profile.yaml，再写盘并热加载。"""
    from core.config import load_secrets, save_profile, save_secrets

    data = load_profile().model_dump()
    data.update(
        {
            "target_keywords": _split_lines(keywords),
            "cities": _parse_cities(cities),
            "score_threshold": score_threshold,
            "per_keyword_max_pages": per_keyword_max_pages,
            "detail_page_limit": detail_page_limit,
            "min_delay_seconds": min_delay_seconds,
            "max_delay_seconds": max_delay_seconds,
            "collect_mode": collect_mode,
            "hard_filter_enabled": bool(hard_filter_enabled),
            "notify": {
                "enabled": {
                    "feishu": bool(notify_feishu),
                    "serverchan": bool(notify_serverchan),
                }
            },
        }
    )
    data["schedule"] = {**data.get("schedule", {}), "cron": _split_lines(cron)}
    data["llm"] = {
        **data.get("llm", {}),
        "base_url": llm_base_url.strip(),
        "model": llm_model.strip(),
    }

    try:
        save_profile(data)
    except Exception as e:  # noqa: BLE001 - 校验失败要把原因反馈给用户
        logger.warning("保存 profile 失败：{}", e)
        return RedirectResponse(f"/settings?err=保存失败：{e}", status_code=303)

    old = load_secrets()
    save_secrets(
        {
            "llm_api_key": api_key.strip() or old.llm_api_key,
            "feishu_webhook": feishu_webhook.strip() or old.feishu_webhook,
            "serverchan_sendkey": serverchan_sendkey.strip() or old.serverchan_sendkey,
        }
    )
    return RedirectResponse("/settings?msg=已保存并热加载，下一轮任务即生效", status_code=303)


@app.post("/api/test-llm")
def api_test_llm() -> dict:
    """测试 LLM 连通性。"""
    from core.llm import test_llm

    ok, detail = test_llm()
    return {"ok": ok, "message": "连通正常" if ok else f"失败：{detail}"}


@app.post("/api/test-feishu")
def api_test_feishu() -> dict:
    """向已填写的推送渠道各发一条测试消息。"""
    try:
        from notify.push import test_push

        detail = test_push()
        ok = "已发送" in detail or "已收到" in detail
        return {"ok": ok, "message": detail}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "message": f"测试失败：{e}"}


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request) -> HTMLResponse:
    """运行日志：每轮采集/打分/话术的计数与错误信息。"""
    runs = list_runlogs(limit=100)
    durations = {}
    for run in runs:
        if run.finished_at and run.started_at:
            durations[run.id] = int((run.finished_at - run.started_at).total_seconds())
    return render(request, "runs.html", {"active": "runs", "runs": runs, "durations": durations})
