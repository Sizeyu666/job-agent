"""JobAgent 命令行入口。

用法：
    python main.py login       # 打开浏览器，手动扫码登录 BOSS，登录态自动保存
    python main.py serve       # 启动 Web 平台（含定时调度）
    python main.py run-once    # 命令行跑一轮采集→打分→话术→推送（调试用）
"""

from __future__ import annotations

import argparse
import sys


def _ensure_console_utf8() -> None:
    """Windows 控制台默认 GBK，统一切到 UTF-8，避免中文日志乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 - 非交互式流不支持 reconfigure 时忽略
            pass


def cmd_login(_args: argparse.Namespace) -> int:
    """打开有头浏览器，等待用户手动完成 BOSS 登录。"""
    from core.config import ensure_dirs
    from core.log import setup_logging

    ensure_dirs()
    setup_logging()
    from browser.session import interactive_login

    interactive_login()
    return 0


def cmd_serve(_args: argparse.Namespace) -> int:
    """启动 FastAPI Web 平台（127.0.0.1:8000）。"""
    from core.config import ensure_dirs, load_profile
    from core.log import setup_logging

    ensure_dirs()
    setup_logging()
    profile = load_profile()

    import uvicorn

    from web.app import app

    print(f"JobAgent 已启动：http://{profile.web.host}:{profile.web.port}")
    uvicorn.run(app, host=profile.web.host, port=profile.web.port, log_level="warning")
    return 0


def cmd_run_once(_args: argparse.Namespace) -> int:
    """命令行执行一轮完整流水线。"""
    from core.config import ensure_dirs
    from core.log import setup_logging

    ensure_dirs()
    setup_logging()
    from core.pipeline import run_once

    run = run_once(trigger="cli")
    print(
        f"本轮结束：状态={run.state} 新岗位={run.new_jobs} 已打分={run.evaluated} "
        f"推荐={run.recommended} 可考虑={run.candidate} 淘汰={run.rejected} 话术={run.greeted_count}"
    )
    if run.message:
        print(f"说明：{run.message}")
    return 0 if run.state in ("success", "paused_captcha") else 1


def main() -> int:
    """解析子命令并执行。"""
    _ensure_console_utf8()
    parser = argparse.ArgumentParser(prog="job-agent", description="JobAgent 求职副驾驶")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="登录 BOSS（弹出浏览器手动扫码）")
    p_login.set_defaults(func=cmd_login)

    p_serve = sub.add_parser("serve", help="启动 Web 平台")
    p_serve.set_defaults(func=cmd_serve)

    p_run = sub.add_parser("run-once", help="命令行跑一轮流水线")
    p_run.set_defaults(func=cmd_run_once)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
