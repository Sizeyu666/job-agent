"""推送（P6）：飞书自定义机器人 Webhook / Server 酱。

- 只在 `profile.notify.enabled` 打开对应渠道时发送；
- 任何网络/格式异常都只写日志，绝不影响流水线结果；
- 只推汇总与告警，不包含任何投递/打招呼动作。
"""

from __future__ import annotations

import httpx
from loguru import logger

from core.config import load_profile, load_secrets
from core.models import RunLog


def build_message(run: RunLog) -> str:
    """按运行结果生成推送正文。"""
    port = load_profile().web.port
    base = f"http://127.0.0.1:{port}"
    if run.state == "paused_captcha":
        head = "JobAgent 告警：检测到验证码/风控，本轮已暂停，请人工处理后重跑"
    elif run.state == "failed":
        head = f"JobAgent 本轮失败：{run.message or '未知原因'}"
    else:
        head = (
            f"JobAgent 本轮完成：新抓取 {run.new_jobs} 个｜推荐 {run.recommended}｜"
            f"可考虑 {run.candidate}｜淘汰 {run.rejected}｜话术 {run.greeted_count}"
        )
    return f"{head}。平台地址 {base}"


def send_feishu(text: str, webhook: str | None = None) -> tuple[bool, str]:
    """发飞书文本消息，返回 (是否成功, 说明)。"""
    hook = (webhook if webhook is not None else load_secrets().feishu_webhook).strip()
    if not hook:
        return False, "未配置飞书 webhook（设置页填写）"
    try:
        resp = httpx.post(
            hook,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=10,
        )
        body = resp.json() if resp.content else {}
        code = body.get("code", body.get("StatusCode", 0))
        if resp.status_code == 200 and code in (0, None):
            return True, "飞书已发送"
        return False, f"飞书返回 HTTP {resp.status_code}：{body}"
    except Exception as e:  # noqa: BLE001 - 推送失败不影响主流程
        return False, f"飞书请求异常：{e}"


def send_serverchan(title: str, desp: str, sendkey: str | None = None) -> tuple[bool, str]:
    """发 Server 酱消息，返回 (是否成功, 说明)。"""
    key = (sendkey if sendkey is not None else load_secrets().serverchan_sendkey).strip()
    if not key:
        return False, "未配置 Server 酱 SendKey（设置页填写）"
    try:
        resp = httpx.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title[:32], "desp": desp},
            timeout=10,
        )
        body = resp.json() if resp.content else {}
        if resp.status_code == 200 and body.get("code") in (0, None):
            return True, "Server 酱已发送"
        return False, f"Server 酱返回 HTTP {resp.status_code}：{body}"
    except Exception as e:  # noqa: BLE001
        return False, f"Server 酱请求异常：{e}"


def push_run_summary(run: RunLog | None) -> dict[str, tuple[bool, str]]:
    """每轮结束后推送汇总；未开启渠道时静默跳过。"""
    if run is None:
        return {}
    profile = load_profile()
    enabled = profile.notify.enabled or {}
    text = build_message(run)
    results: dict[str, tuple[bool, str]] = {}

    if enabled.get("feishu"):
        results["feishu"] = send_feishu(text)
    if enabled.get("serverchan"):
        title = "JobAgent 告警" if run.state in ("failed", "paused_captcha") else "JobAgent 本轮完成"
        results["serverchan"] = send_serverchan(title, text)

    for channel, (ok, detail) in results.items():
        if ok:
            logger.info("推送成功（{}）：{}", channel, detail)
        else:
            logger.warning("推送失败（{}）：{}", channel, detail)
    if not results:
        logger.debug("未开启任何推送渠道，本轮不推送")
    return results


def test_push() -> str:
    """设置页「测试推送」：给已填写的渠道各发一条测试消息。"""
    text = f"JobAgent 推送测试：如果你收到这条消息，说明渠道配置正常。平台地址 http://127.0.0.1:{load_profile().web.port}"
    results = [
        send_feishu(text),
        send_serverchan("JobAgent 推送测试", text),
    ]
    detail = "；".join(r[1] for r in results)
    return detail
