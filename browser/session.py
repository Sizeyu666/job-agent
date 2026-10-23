"""Playwright 浏览器会话：登录态持久化（storage_state）、登录检测、验证码检测。

实测结论（2026-10-23，均已验证）：
1. BOSS 会主动 window.close() 由 Playwright 启动的**有头**窗口（headless=False 时
   搜职位页经 security.html 后必然变成 about:blank 或退回上一页），因此日常采集用无头模式；
2. playwright-stealth 1.0.6 会破坏 navigator.userAgent，导致登录页永远「加载中」，已弃用；
3. 人工扫码登录改用「真实 Edge + --remote-debugging-port 通过 CDP 连接」：
   窗口是用户日常的浏览器（navigator.webdriver=False），不会被关闭，
   用户可以直接在窗口里完成安全验证并扫码，登录后导出 storage_state 供无头采集使用。

红线：本模块只做「打开页面 / 读取页面 / 登录入口点击」，不点击任何沟通、投递、收藏、发送按钮；
遇到安全验证一律交给人手动完成，代码不尝试绕过。
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import time
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from loguru import logger
from playwright.sync_api import Error as PWError
from playwright.sync_api import Page, sync_playwright

from core.config import DATA_DIR, LOG_DIR, ensure_dirs, load_profile

STATE_FILE = DATA_DIR / "browser_state.json"
HOME_URL = "https://www.zhipin.com/"
# 直接打开登录页会被 BOSS 主动关闭成 about:blank，必须从职位搜索页进入：
# 未登录时搜索页会经安全校验后自动跳转到登录页（二维码正常渲染）。
LOGIN_ENTRY_URL = "https://www.zhipin.com/web/geek/jobs?query={query}&city={city}"
LOGIN_TIMEOUT_SECONDS = 300  # 手动扫码登录最多等待 5 分钟

# 与真实 Chrome 一致的 UA，避免 Chromium 默认 UA 的自动化特征被风控标记
REAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 验证码特征（选择器集中维护，页面改版后只改这里）
CAPTCHA_SELECTORS = [
    "#nc_1_wrapper",
    ".captcha-wrap",
    ".nc-container",
    ".geetest",
    ".geetest_panel",
    ".verify-wrap",
    ".boss-popup .verify-box",
    "iframe[src*='captcha']",
]
CAPTCHA_TEXTS = [
    "安全验证",
    "滑块验证",
    "请完成验证",
    "当前 IP 地址可能存在异常访问行为",
    "您的账号可能存在异常访问行为",
    "访问受限",
    "您的 IP 存在异常行为",
]

# 登录态判断：出现任一用户区元素即视为已登录；出现登录按钮即视为未登录
LOGGED_IN_SELECTORS = [
    ".nav-figure",
    ".user-nav",
    ".header-user",
    ".nav-user",
    "a[href*='/web/geek/chat']",
]
LOGGED_OUT_SELECTORS = [
    ".nav-login",
    ".login-register",
    ".login-box",
    ".sign-content",
]


class CaptchaError(RuntimeError):
    """检测到验证码/滑块，必须暂停本轮任务等待人工处理。"""

    def __init__(self, message: str, screenshot: str = ""):
        super().__init__(message)
        self.screenshot = screenshot


class LoginRequired(RuntimeError):
    """BOSS 登录态失效。"""


def random_sleep(min_seconds: int, max_seconds: int) -> None:
    """拟人化随机等待。"""
    seconds = random.uniform(min_seconds, max_seconds)
    logger.debug("随机等待 {:.1f} 秒", seconds)
    time.sleep(seconds)


def apply_stealth(page: Page) -> None:
    """最小化反检测：仅隐藏 webdriver 标记（不使用 playwright-stealth）。"""
    try:
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
    except Exception as e:  # noqa: BLE001 - 反检测增强失败只记录
        logger.warning("应用反检测脚本失败：{}", e)


class BrowserSession:
    """普通 Chromium 会话，登录态通过 data/browser_state.json 持久化。"""

    def __init__(
        self,
        headless: Optional[bool] = None,
        device_scale_factor: int = 1,
        channel: Optional[str] = "msedge",
    ):
        profile = load_profile()
        self.headless = profile.browser_headless if headless is None else headless
        # 截图放大倍数：登录二维码需要 2 倍像素才能被手机稳定扫到
        self.device_scale_factor = device_scale_factor
        # 优先用真实 Edge 内核（channel=msedge），指纹比 Playwright 自带 Chromium 更接近真实用户
        self.channel = channel
        self._playwright = None
        self.browser = None
        self.context = None
        self.page: Optional[Page] = None

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> Page:
        """启动 Chromium 并创建上下文（有登录态文件则自动加载）。"""
        ensure_dirs()
        self._playwright = sync_playwright().start()
        launch_kwargs = dict(
            headless=self.headless,
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
        )
        try:
            self.browser = self._playwright.chromium.launch(channel=self.channel, **launch_kwargs)
            logger.info("使用浏览器内核：{}", self.channel)
        except PWError as e:
            logger.warning("channel={} 启动失败，回退到内置 Chromium：{}", self.channel, e)
            self.browser = self._playwright.chromium.launch(**launch_kwargs)
        storage = str(STATE_FILE) if STATE_FILE.exists() else None
        self.context = self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=REAL_USER_AGENT,
            storage_state=storage,
            device_scale_factor=self.device_scale_factor,
        )
        self.context.set_default_timeout(30000)
        self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self.page = self.context.new_page()
        apply_stealth(self.page)
        logger.info("浏览器已启动（headless={}，登录态文件={}）", self.headless, bool(storage))
        return self.page

    def save_state(self) -> None:
        """保存 cookies + localStorage 到 data/browser_state.json。"""
        if not self.context:
            return
        try:
            self.context.storage_state(path=str(STATE_FILE))
            logger.info("登录态已保存：{}", STATE_FILE)
        except Exception as e:  # noqa: BLE001
            logger.warning("保存登录态失败：{}", e)

    def close(self) -> None:
        """关闭浏览器与 Playwright（异常仅记录）。"""
        try:
            if self.context:
                self.context.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("关闭浏览器上下文失败：{}", e)
        try:
            if self.browser:
                self.browser.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("关闭浏览器失败：{}", e)
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception as e:  # noqa: BLE001
            logger.warning("停止 Playwright 失败：{}", e)
        finally:
            self.context = None
            self.browser = None
            self.page = None
            self._playwright = None

    def new_page(self) -> Page:
        """新建标签页并应用反检测脚本。"""
        if not self.context:
            raise RuntimeError("浏览器会话未启动")
        page = self.context.new_page()
        apply_stealth(page)
        return page


def _save_captcha_shot(page: Page) -> str:
    """保存验证码截图到 logs/，返回路径字符串。"""
    ensure_dirs()
    shot = LOG_DIR / f"captcha_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    try:
        page.screenshot(path=str(shot), full_page=False)
        logger.warning("已保存验证码截图：{}", shot)
    except Exception as e:  # noqa: BLE001
        logger.error("保存验证码截图失败：{}", e)
        return ""
    return str(shot)


def detect_captcha(page: Page, raise_error: bool = True, save_shot: bool = True) -> bool:
    """检测安全验证/滑块页面；命中则截图并抛 CaptchaError（绝不尝试绕过）。"""
    hit = False
    for selector in CAPTCHA_SELECTORS:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                logger.warning("检测到验证码元素：{}", selector)
                hit = True
                break
        except Exception:  # noqa: BLE001 - 元素查询期间页面跳转属正常
            continue

    if not hit:
        try:
            title = page.title() or ""
            body = page.inner_text("body")[:1500]
            text = title + body
            for keyword in CAPTCHA_TEXTS:
                if keyword in text:
                    logger.warning("检测到验证码文案：{}", keyword)
                    hit = True
                    break
        except Exception:  # noqa: BLE001
            pass

    if hit:
        shot = _save_captcha_shot(page) if save_shot else ""
        if raise_error:
            raise CaptchaError("BOSS 出现安全验证/滑块，本轮任务暂停，请人工处理", screenshot=shot)
    return hit


def is_logged_in(page: Page) -> bool:
    """判断 BOSS 当前是否已登录。"""
    try:
        url = page.url or ""
    except Exception:  # noqa: BLE001 - 页面刚被关闭
        return False
    if "/web/user/" in url or "passport" in url or url == "about:blank":
        return False

    # 页面文案最可靠：出现「登录/注册」一定未登录，出现用户入口一定已登录
    try:
        body = page.inner_text("body")[:4000]
        if "登录/注册" in body:
            return False
        if "我的简历" in body or "退出登录" in body:
            return True
    except Exception:  # noqa: BLE001
        pass

    for selector in LOGGED_OUT_SELECTORS:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                return False
        except Exception:  # noqa: BLE001
            continue
    for selector in LOGGED_IN_SELECTORS:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def open_home(page: Page) -> None:
    """打开 BOSS 首页并做基础等待。"""
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)


def ensure_login(page: Page) -> None:
    """采集前确认登录态；未登录或遇到验证码时抛异常终止本轮。"""
    if not STATE_FILE.exists():
        raise LoginRequired("尚未登录 BOSS，请先运行 python main.py login 扫码登录")
    open_home(page)
    detect_captcha(page)
    if not is_logged_in(page):
        raise LoginRequired("BOSS 登录态已失效，请重新运行 python main.py login 扫码登录")
    logger.info("BOSS 登录态有效")


def _active_page(session: BrowserSession, fallback: Page) -> Page:
    """取当前活动页面（登录可能打开新标签页时取最新一个）。"""
    try:
        if session.context and session.context.pages:
            return session.context.pages[-1]
    except Exception:  # noqa: BLE001
        pass
    return fallback


# 真实 Edge + CDP：BOSS 一检测到 DevTools 协议就立刻让页面失效（跳 about:blank），
# 因此登录只启动「完全不受控」的普通 Edge 窗口，靠读取 profile 里的 Cookie 判断登录结果。
CDP_URL = "http://127.0.0.1:9222"
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
REAL_PROFILE_DIR = DATA_DIR / "edge_profile"


def _find_edge() -> str:
    """返回本机 Edge 可执行文件路径，找不到返回空串。"""
    for path in EDGE_PATHS:
        if os.path.exists(path):
            return path
    return ""


def _launch_edge_window(url: str) -> Optional[subprocess.Popen]:
    """以用户日常方式启动 Edge 窗口（不开调试端口，BOSS 无法判定为自动化）。"""
    exe = _find_edge()
    if not exe:
        logger.error("未找到 Microsoft Edge")
        return None
    ensure_dirs()
    try:
        proc = subprocess.Popen(
            [
                exe,
                f"--user-data-dir={REAL_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
                url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("已打开 Edge 登录窗口（pid={}）", proc.pid)
        return proc
    except Exception as e:  # noqa: BLE001
        logger.error("启动 Edge 失败：{}", e)
        return None


def _export_state_from_profile() -> bool:
    """用无头 Edge 打开同一 profile 直接导出 storage_state。

    注意：这里**不访问 BOSS**。cookie 是从 profile 本地读出来的，
    任何一次多余的页面访问都会叠加平台风控，这正是之前 IP 被封的原因。
    """
    try:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                str(REAL_PROFILE_DIR),
                channel="msedge",
                headless=True,
            )
            try:
                ctx.storage_state(path=str(STATE_FILE))
            finally:
                ctx.close()

        cookies = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("cookies", [])
        zhipin = [c for c in cookies if "zhipin" in (c.get("domain") or "")]
        logger.info("登录态已导出：{}（zhipin cookie {} 个）", STATE_FILE, len(zhipin))
        return len(zhipin) > 0
    except Exception as e:  # noqa: BLE001
        logger.error("导出登录态失败：{}", e)
        return False


def _kill_process_tree(pid: int) -> None:
    """关闭我们自己启动的 Edge 进程树（不动用户其他浏览器窗口）。"""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("关闭 Edge 进程失败：{}", e)


def interactive_login() -> bool:
    """打开普通 Edge 窗口到 BOSS 登录页，等用户手动完成验证并扫码，然后导出登录态。

    BOSS 一旦检测到 CDP/DevTools 协议就立刻把页面跳成 about:blank，
    所以这里完全不用 Playwright 驱动登录窗口，只读 profile 里的 Cookie 判断是否登录成功。
    代码只做读取与等待，绝不自动绕过安全验证。
    """
    ensure_dirs()
    profile = load_profile()
    entry_url = LOGIN_ENTRY_URL.format(
        query=quote(profile.target_keywords[0]),
        city=profile.cities[0].code,
    )

    proc = _launch_edge_window(entry_url)
    if proc is None:
        print("无法打开 Edge，请确认本机已安装 Microsoft Edge。")
        return False

    print("=" * 64)
    print("已打开登录窗口（屏幕上的 Edge 就是）：")
    print("  1. 若出现「安全验证」，请在窗口里手动点按钮完成验证；")
    print("  2. 进入登录页后，请扫码或用验证码登录 BOSS 直聘；")
    print("  3. 登录成功后，直接关闭这个 Edge 窗口，程序会自动保存登录态。")
    print(f"最多等 {LOGIN_TIMEOUT_SECONDS // 60} 分钟。")
    print("=" * 64)
    logger.info("等待用户手动登录 BOSS（关闭窗口即视为完成）…")

    deadline = time.time() + LOGIN_TIMEOUT_SECONDS
    last_hint = 0.0
    try:
        while proc.poll() is None and time.time() < deadline:
            now = time.time()
            if now - last_hint > 30:
                print("登录完成后请关闭该 Edge 窗口；程序会自动导出登录态。")
                last_hint = now
            time.sleep(3)

        if proc.poll() is None:
            logger.warning("等待登录超时")
            print("等待登录超时：窗口保持打开，登录完成后可重跑 python main.py login。")
            return False

        print("窗口已关闭，正在本地导出登录态（不会访问 BOSS，避免触发风控）…")
        time.sleep(2)
        if not _export_state_from_profile():
            print("登录态导出失败，请重跑 python main.py login。")
            return False

        print("=" * 64)
        print("登录态已保存到 data/browser_state.json。")
        print("=" * 64)
        return True
    finally:
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
