"""BOSS 直聘采集器（只读）。

要点：
1. 优先拦截搜索接口 /wapi/zpgeek/search/joblist.json 的响应，结构化取数；
2. 接口不可用时降级为 DOM 解析（选择器集中在本类 SELECTORS 常量）；
3. 详情页通过 page.goto 正常浏览，绝不点击「立即沟通 / 投递 / 收藏 / 发送」；
4. 所有动作之间加入随机延迟，单岗位异常隔离，验证码立即终止。
"""

from __future__ import annotations

import random
import re
import time
from typing import Callable, Optional
from urllib.parse import quote

from loguru import logger
from playwright.sync_api import Page, TimeoutError as PWTimeoutError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from browser.session import CaptchaError, detect_captcha
from core.config import Profile
from platforms.base import PlatformAdapter

JOB_DETAIL_ID_RE = re.compile(r"/job_detail/([^/?#]+)\.html")


class BossAdapter(PlatformAdapter):
    """BOSS 直聘岗位采集适配器。"""

    BASE_URL = "https://www.zhipin.com"
    SEARCH_URL = "https://www.zhipin.com/web/geek/job?query={query}&city={city}&page={page}"
    SEARCH_API_KEY = "/wapi/zpgeek/search/joblist.json"

    # 选择器集中维护：BOSS 改版后只改这里（每项均为候选列表，取第一个命中的）
    SELECTORS: dict[str, list[str]] = {
        # 列表页
        "card": ["li.job-card-wrapper", ".job-card-wrapper", ".job-list-box > li"],
        "card_link": ["a.job-name", ".job-card-left a[href*='/job_detail/']", "a[href*='/job_detail/']"],
        "title": [".job-name", ".job-title", ".job-card-left .job-name"],
        "company": [".company-name", ".job-card-right .company-name"],
        "area": [".job-area", ".job-area-wrapper"],
        "salary": [".salary", ".job-salary"],
        "card_tags": [".job-card-footer .tag-list li", ".job-info .tag-list li", ".tag-list li"],
        # 详情页
        "detail_jd": [
            ".job-sec-text",
            ".job-detail-section .job-sec-text",
            ".detail-content .job-sec-text",
            ".job-detail .text",
        ],
        "detail_title": [".job-banner .name h1", ".job-banner .name", ".job-primary .name h1"],
        "detail_salary": [".job-banner .salary", ".job-primary .salary"],
        "detail_company": [".company-info a", ".company-info .company-name", ".job-banner .company-info"],
        "detail_tags": [".job-keyword-list li", ".job-tags span", ".job-detail-section .tag-list li"],
        "detail_labels": [".job-banner .info-primary p", ".job-primary .job-info p"],
    }

    def __init__(self, page: Page, profile: Optional[Profile] = None):
        from core.config import load_profile

        self.page = page
        self.profile = profile or load_profile()

    # ---------------- 基础工具 ----------------
    def _sleep(self, min_seconds: Optional[int] = None, max_seconds: Optional[int] = None) -> None:
        """拟人化随机等待。"""
        lo = self.profile.min_delay_seconds if min_seconds is None else min_seconds
        hi = self.profile.max_delay_seconds if max_seconds is None else max_seconds
        seconds = random.uniform(lo, hi)
        logger.debug("随机等待 {:.1f} 秒", seconds)
        time.sleep(seconds)

    @staticmethod
    def _is_retryable(exc: BaseException) -> bool:
        """验证码/登录问题不重试，网络类错误才重试。"""
        return not isinstance(exc, (CaptchaError,))

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=2, min=2, max=8),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def _goto(self, url: str) -> None:
        """访问页面（失败重试 2 次）。"""
        self.page.goto(url, wait_until="domcontentloaded", timeout=45000)

    def _first_text(self, scope, key: str) -> str:
        """按候选选择器取第一个非空文本。"""
        for selector in self.SELECTORS[key]:
            try:
                el = scope.query_selector(selector)
                if el:
                    text = (el.inner_text() or "").strip()
                    if text:
                        return text
            except Exception:  # noqa: BLE001 - 单个选择器失败不影响整体
                continue
        return ""

    def _first_attr(self, scope, key: str, attr: str) -> str:
        """按候选选择器取第一个非空属性值。"""
        for selector in self.SELECTORS[key]:
            try:
                el = scope.query_selector(selector)
                if el:
                    value = el.get_attribute(attr)
                    if value:
                        return value
            except Exception:  # noqa: BLE001
                continue
        return ""

    def _all_texts(self, scope, key: str) -> list[str]:
        """按第一个命中的选择器取全部文本（用于标签列表）。"""
        for selector in self.SELECTORS[key]:
            try:
                els = scope.query_selector_all(selector)
                texts = [(el.inner_text() or "").strip() for el in els]
                texts = [t for t in texts if t]
                if texts:
                    return texts
            except Exception:  # noqa: BLE001
                continue
        return []

    # ---------------- 搜索 ----------------
    def search(self, keyword: str, city_code: str, max_pages: int) -> list[dict]:
        """按关键词 + 城市搜索，最多翻 max_pages 页，返回岗位列表。"""
        results: list[dict] = []
        seen_ids: set[str] = set()

        for page_no in range(1, max_pages + 1):
            url = self.SEARCH_URL.format(
                query=quote(keyword), city=city_code, page=page_no
            )
            logger.info("搜索岗位：关键词={} 城市={} 第 {} 页", keyword, city_code, page_no)

            captured: list = []

            def _on_response(response, bucket=captured) -> None:
                """仅记录目标接口响应，不在回调里做任何写操作。"""
                try:
                    if self.SEARCH_API_KEY in response.url:
                        bucket.append(response)
                except Exception:  # noqa: BLE001
                    pass

            self.page.on("response", _on_response)
            try:
                self._goto(url)
                detect_captcha(self.page)
                self.page.wait_for_timeout(2500)
                self._sleep()

                items = self._parse_xhr(captured)
                if items:
                    logger.info("第 {} 页通过接口解析到 {} 个岗位", page_no, len(items))
                else:
                    logger.warning("第 {} 页接口数据不可用，降级为 DOM 解析", page_no)
                    items = self._parse_dom()
                    logger.info("第 {} 页 DOM 解析到 {} 个岗位", page_no, len(items))
            finally:
                try:
                    self.page.remove_listener("response", _on_response)
                except Exception:  # noqa: BLE001
                    pass

            new_count = 0
            for item in items:
                job_id = item.get("job_id")
                if not job_id or job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                results.append(item)
                new_count += 1
            logger.info("第 {} 页新增 {} 个岗位（累计 {}）", page_no, new_count, len(results))

            if not items:
                logger.info("第 {} 页没有结果，停止翻页", page_no)
                break

        return results

    def _parse_xhr(self, responses: list) -> list[dict]:
        """解析搜索接口 JSON；字段缺失或加密时返回空列表以触发 DOM 降级。"""
        for resp in responses:
            try:
                data = resp.json()
            except Exception as e:  # noqa: BLE001 - 接口响应可能不可读
                logger.debug("读取接口响应失败：{}", e)
                continue
            code = data.get("code")
            message = str(data.get("message") or "")
            # code=37「您的环境存在异常」等属于平台风控，直接暂停本轮，不要反复重试
            if code not in (0, None) and ("异常" in message or "受限" in message or code == 37):
                raise CaptchaError(f"BOSS 接口拒绝访问（code={code}，{message}），本轮任务暂停")
            try:
                zp_data = (data or {}).get("zpData") or {}
                job_list = zp_data.get("jobList") or zp_data.get("jobListV2") or []
                if not isinstance(job_list, list) or not job_list:
                    continue
                items = []
                for raw in job_list:
                    item = self._map_api_item(raw)
                    if item:
                        items.append(item)
                if items:
                    return items
            except Exception as e:  # noqa: BLE001
                logger.warning("解析接口 JSON 结构失败，转 DOM 解析：{}", e)
        return []

    def _map_api_item(self, raw: dict) -> Optional[dict]:
        """把接口返回的岗位对象映射为统一 dict。"""
        try:
            job_id = str(raw.get("encryptJobId") or raw.get("jobId") or "").strip()
            if not job_id:
                return None
            tags: list[str] = []
            for key in ("skills", "jobLabels", "jobTags"):
                value = raw.get(key)
                if isinstance(value, list):
                    tags.extend(str(x).strip() for x in value if str(x).strip())
            city = "·".join(
                x for x in (raw.get("cityName"), raw.get("areaDistrict"), raw.get("businessDistrict")) if x
            )
            return {
                "platform": "boss",
                "job_id": job_id,
                "url": f"{self.BASE_URL}/job_detail/{job_id}.html",
                "title": (raw.get("jobName") or "").strip(),
                "company": (raw.get("brandName") or "").strip(),
                "salary_raw": (raw.get("salaryDesc") or "").strip(),
                "city": city,
                "experience": (raw.get("jobExperience") or "").strip(),
                "education": (raw.get("jobDegree") or "").strip(),
                "tags": list(dict.fromkeys(tags))[:12],
                "jd_text": "",
            }
        except Exception as e:  # noqa: BLE001 - 单条数据异常隔离
            logger.warning("映射接口岗位失败：{}", e)
            return None

    def _parse_dom(self) -> list[dict]:
        """DOM 降级解析岗位卡片。"""
        items: list[dict] = []
        cards = []
        for selector in self.SELECTORS["card"]:
            try:
                cards = self.page.query_selector_all(selector)
            except Exception:  # noqa: BLE001
                cards = []
            if cards:
                break

        for card in cards:
            try:
                href = ""
                for selector in self.SELECTORS["card_link"]:
                    el = card.query_selector(selector)
                    if el:
                        href = el.get_attribute("href") or ""
                        if href:
                            break
                if not href:
                    continue
                url = href if href.startswith("http") else f"{self.BASE_URL}{href}"
                match = JOB_DETAIL_ID_RE.search(url)
                if not match:
                    continue
                job_id = match.group(1)

                title = self._first_text(card, "title")
                if not title:
                    continue
                company = self._first_text(card, "company")
                salary = self._first_text(card, "salary")
                area = self._first_text(card, "area")
                raw_tags = self._all_texts(card, "card_tags")
                experience, education, skill_tags = self._split_tags(raw_tags)

                items.append(
                    {
                        "platform": "boss",
                        "job_id": job_id,
                        "url": url,
                        "title": title,
                        "company": company,
                        "salary_raw": salary,
                        "city": area,
                        "experience": experience,
                        "education": education,
                        "tags": skill_tags[:12],
                        "jd_text": "",
                    }
                )
            except Exception as e:  # noqa: BLE001 - 单个卡片异常隔离
                logger.warning("解析岗位卡片失败：{}", e)
                continue
        return items

    @staticmethod
    def _split_tags(raw_tags: list[str]) -> tuple[str, str, list[str]]:
        """从标签列表里拆分经验、学历与技能标签。"""
        experience, education, skills = "", "", []
        degree_words = ("本科", "硕士", "大专", "博士", "学历不限", "中专", "高中")
        for tag in raw_tags:
            if not tag:
                continue
            if not experience and ("年" in tag or "经验" in tag):
                experience = tag
            elif not education and tag in degree_words:
                education = tag
            else:
                skills.append(tag)
        return experience, education, skills

    # ---------------- 详情页 ----------------
    def fetch_detail(self, page: Page, item: dict) -> dict:
        """浏览岗位详情页，抓取 JD 正文与完整标签（不点击任何按钮）。"""
        url = item.get("url") or ""
        if not url:
            return item
        try:
            self._goto(url)
            detect_captcha(page)
            page.wait_for_timeout(1800)
            self._sleep(2, 5)

            jd_parts: list[str] = []
            for selector in self.SELECTORS["detail_jd"]:
                try:
                    el = page.query_selector(selector)
                    if el:
                        text = (el.inner_text() or "").strip()
                        if text:
                            jd_parts.append(text)
                except Exception:  # noqa: BLE001
                    continue
            if jd_parts:
                item["jd_text"] = "\n\n".join(dict.fromkeys(jd_parts))

            title = self._first_text(page, "detail_title")
            if title:
                item["title"] = title
            salary = self._first_text(page, "detail_salary")
            if salary:
                item["salary_raw"] = salary
            company = self._first_text(page, "detail_company")
            if company:
                item["company"] = company

            detail_tags = self._all_texts(page, "detail_tags")
            if detail_tags:
                merged = list(dict.fromkeys([*(item.get("tags") or []), *detail_tags]))
                item["tags"] = merged[:15]

            labels = self._all_texts(page, "detail_labels")
            if labels:
                exp, edu, _ = self._split_tags(labels)
                if exp and not item.get("experience"):
                    item["experience"] = exp
                if edu and not item.get("education"):
                    item["education"] = edu

            if not item.get("jd_text"):
                logger.warning("详情页未抓到 JD 正文：{}", url)
            return item
        except CaptchaError:
            raise
        except (PWTimeoutError, Exception) as e:  # noqa: BLE001 - 单岗位失败隔离，不中断整轮
            logger.warning("抓取岗位详情失败（{}）：{}", url, e)
            return item


def make_response_handler(bucket: list) -> Callable:
    """构造响应拦截回调（供需要外部拦截时复用）。"""

    def _handler(response) -> None:
        try:
            if BossAdapter.SEARCH_API_KEY in response.url:
                bucket.append(response)
        except Exception:  # noqa: BLE001
            pass

    return _handler
