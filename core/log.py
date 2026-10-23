"""日志初始化：控制台 + 按天写入 logs/ 的 loguru 日志。"""

from __future__ import annotations

import sys

from loguru import logger

from core.config import LOG_DIR


def setup_logging(level: str = "INFO") -> None:
    """初始化 loguru：彩色控制台输出 + 按天轮转文件日志（UTF-8）。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{name}</cyan> - {message}",
    )
    logger.add(
        LOG_DIR / "jobagent_{time:YYYY-MM-DD}.log",
        level=level,
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
        enqueue=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {name}:{line} - {message}",
    )
    logger.debug("日志系统已初始化")
