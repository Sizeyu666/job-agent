# Job Agent 开发任务书（交给 TRAE 执行）

> 你是本项目的开发工程师。请先读完本文件与 `.trae/rules/project_rules.md`，然后严格按「第七节 开发顺序」分阶段实现，每完成一个阶段先自测再继续。环境与依赖已由他人配置完成（Python 3.11 虚拟环境在 `.venv/`，依赖已装），你不需要重新安装环境。

---

## 一、项目目标

本地运行的「求职副驾驶」Web 平台（名字：JobAgent）。它每天定时自动完成：

1. 在 **BOSS 直聘**搜索用户指定的目标岗位关键词（如「AI 应用工程师」「大模型应用开发实习」）；
2. 抓取岗位列表与详情页 JD，存入本地 SQLite 并自动去重；
3. 用「内置匹配打分 skill」（向量相似度 + 技能覆盖 + LLM 语义打分）对用户简历与每个岗位逐一打分；
4. 为每个合格岗位生成 **3 条定制化打招呼话术**（结合 JD 要求与简历中的具体项目经历）；
5. 在本地 Web 平台展示岗位卡片、匹配分、匹配理由、话术，并提供「去 BOSS 打招呼」直链；
6. 每轮运行后通过飞书机器人推送汇总；
7. 支持开机自启 / 定时运行，网页上可查看任务进度与历史日志。

### 严格红线（最重要）

- **本系统只采集和浏览，绝不执行任何写操作**：禁止点击「立即沟通」「投递简历」「收藏」「发送」等任何按钮，禁止向 BOSS 提交任何表单。投递与打招呼 100% 由用户本人手动完成。
- **禁止尝试绕过验证码 / 滑块 / 风控**。一旦检测到验证页面，立即暂停本轮任务、保存截图、通知用户，等待人工处理。
- 违反以上任意一条的代码都不允许提交。

---

## 二、用户画像（默认值，可在设置中改）

- 南京某高校数据科学与大数据技术专业大三学生，找**实习**；
- 目标岗位：AI 应用工程师 / 大模型应用开发 / AI Agent 开发 / Python 后端实习；
- 目标城市：南京、上海、杭州、远程；
- 简历为**动态文件**：用户会在网页上传新版 PDF/DOCX/MD，系统必须支持替换与重打分。

---

## 三、技术栈（已安装，版本见 requirements.txt，禁止随意更换）

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11、FastAPI、Uvicorn、Jinja2 |
| 前端 | Jinja2 模板 + Tailwind CSS（CDN）+ Alpine.js（CDN），**零构建、不引入 Node 工程** |
| 数据库 | SQLite + SQLModel |
| 浏览器自动化 | Playwright（Chromium 已下载）、playwright-stealth |
| LLM | openai SDK 走 **OpenAI 兼容接口**（DeepSeek / 豆包 / 通义均可，base_url 可配） |
| 向量匹配 | FastEmbed，模型 `BAAI/bge-small-zh-v1.5`（中文、约 100MB，首次运行自动下载到 `data/models/`） |
| 调度 | APScheduler（内嵌 Web 进程） |
| 简历解析 | pdfplumber（PDF）、python-docx（DOCX）、Markdown 直读 |
| 推送 | 飞书自定义机器人 Webhook；Server 酱备选 |
| 日志 | loguru，按天写入 `logs/` |

---

## 四、目录结构（目录已建好，按此创建文件）

```
job-agent/
├── main.py                     # 入口：login / serve / run-once
├── requirements.txt            # 已就绪，勿改
├── config/
│   ├── profile.yaml            # 实际配置（从 example 复制）
│   ├── profile.example.yaml    # 配置模板（已提供）
│   ├── secrets.yaml            # 密钥（从 example 复制，已被 gitignore）
│   └── secrets.example.yaml
├── core/
│   ├── __init__.py
│   ├── config.py               # 配置加载（pydantic）
│   ├── models.py               # SQLModel 表模型
│   ├── db.py                   # CRUD / 去重
│   ├── resume.py               # 简历读取、解析、结构化
│   ├── pipeline.py             # 采集→打分→话术→推送 主流水线
│   ├── scheduler.py            # APScheduler 定时任务
│   └── prompts.py              # 打分/话术的 LLM prompt 模板
├── matcher/
│   ├── __init__.py
│   └── skill.py                # 内置匹配打分 skill（向量+技能覆盖+LLM）
├── platforms/
│   ├── __init__.py
│   ├── base.py                 # 平台适配器抽象基类
│   └── boss.py                 # BOSS 直聘采集器
├── browser/
│   ├── __init__.py
│   └── session.py              # Playwright 持久化登录态、验证码检测
├── notify/
│   ├── __init__.py
│   └── push.py                 # 飞书 / Server酱
├── web/
│   ├── __init__.py
│   ├── app.py                  # FastAPI 应用与路由
│   ├── templates/              # base/dashboard/jobs/job_detail/resume/settings/runs
│   └── static/                 # 少量自定义 CSS/JS
├── scripts/
│   ├── start.ps1               # 启动平台（已提供）
│   ├── install_scheduled_task.ps1  # 注册开机自启（已提供）
│   └── uninstall_scheduled_task.ps1
├── data/                       # 运行时数据（gitignore）
│   ├── job_agent.db
│   ├── browser_profile/        # 浏览器持久化登录态
│   └── models/                 # FastEmbed 模型缓存
├── resumes/                    # 用户上传的简历（gitignore）
├── output/                     # （可选）导出的 HTML 日报
└── logs/                       # 日志与验证码截图（gitignore）
```

---

## 五、数据库模型（SQLModel）

- **Job**：`id`、`platform`(默认"boss")、`job_id`(平台原始ID)、`url`、`title`、`company`、`salary_raw`、`city`、`experience`、`education`、`jd_text`(全文)、`tags`(JSON数组，如技能标签)、`status`(pending/applied/interviewed/ignored，默认pending)、`first_seen_at`、`last_seen_at`、`is_deleted`；唯一约束 `(platform, job_id)`。
- **Resume**：`id`、`filename`、`file_type`、`raw_text`、`structured_json`、`is_active`(当前生效版本)、`created_at`。
- **Evaluation**：`id`、`job_id`(外键)、`resume_id`、`hard_passed`(bool)、`hard_gaps`(JSON)、`embedding_score`(float 0-100)、`skill_coverage`(float)、`llm_score`(float)、`final_score`(float)、`matched_points`(JSON)、`pitch_angle`(str)、`risk_flags`(JSON)、`jd_summary`(str)、`grade`(recommended/candidate/rejected)、`created_at`；同一 job+resume 唯一。
- **Greeting**：`id`、`job_id`、`resume_id`、`style`(direct/skill/sincere)、`content`、`created_at`。
- **RunLog**：`id`、`started_at`、`finished_at`、`state`(running/success/failed/paused_captcha)、`new_jobs`、`evaluated`、`recommended`、`candidate`、`rejected`、`greeted_count`(生成话术数)、`message`(错误信息)、`captcha_shot`(截图路径)。

---

## 六、各模块规格

### 6.1 配置（core/config.py）

- 启动时加载 `config/profile.yaml`（pydantic 模型校验）与 `config/secrets.yaml`；
- 网页「设置」页可修改 profile.yaml 并热更新（保存后重新加载）；
- profile.yaml 字段含义以 `config/profile.example.yaml` 注释为准。

### 6.2 简历管理（core/resume.py）

- 网页上传的文件保存到 `resumes/`，文件名加时间戳避免覆盖；
- 解析顺序：PDF→pdfplumber（逐页拼接文本）；DOCX→python-docx（段落+表格）；MD/TXT→直读；
- 解析出的纯文本调用 LLM 输出结构化 JSON：`{name, education, skills:[], projects:[{name, stack, duty, result}], experiences, highlights:[]}`，解析失败则只用纯文本（打分降级仍可运行）；
- 新简历上传后置为 active。**默认只对新岗位打分**；网页提供「用最新简历重新打分全部岗位」按钮触发全量重算（后台任务，进度显示在运行日志）。

### 6.3 浏览器会话（browser/session.py）

- `launch_persistent_context(user_data_dir="data/browser_profile", headless=配置项默认false, viewport=1920x1080, locale="zh-CN")`，应用 playwright-stealth；
- `main.py login`：打开 `https://www.zhipin.com/`，等待用户手动扫码/短信登录（轮询页面直到出现登录态标志，超时 5 分钟），登录态由 persistent context 自动保存；
- 每次采集前调用 `ensure_login()`：访问首页，检测是否出现登录按钮 / 跳转登录页；失效则终止本轮并通知「请先运行 python main.py login」；
- `detect_captcha(page)`：检测安全验证/滑块特征（选择器如 `#nc_1_wrapper`、`.captcha-wrap`、`.geetest`、页面文字包含「安全验证」「滑块」，选择器集中维护并尽量多列候选），命中则截图 `logs/captcha_{时间戳}.png`，抛 `CaptchaError`。

### 6.4 BOSS 采集器（platforms/boss.py + base.py）

`base.py` 抽象基类定义：
```python
class PlatformAdapter(ABC):
    def search(self, keyword: str, city_code: str, max_pages: int) -> list[dict]: ...
    def fetch_detail(self, page, item: dict) -> dict: ...
```

BOSS 实现要点：
1. 搜索 URL：`https://www.zhipin.com/web/geek/job?query={urlencode(keyword)}&city={city_code}&page={page}`；
2. **数据获取优先拦截 XHR**：注册 `page.on("response")`，捕获 URL 含 `/wapi/zpgeek/search/joblist.json` 的响应，解析其中的岗位 JSON；若该接口字段加密/不可用，**降级为 DOM 解析**（列表卡片选择器如 `.job-card-wrapper .job-card-left`，以实际页面为准）；
3. 列表提取：岗位名、详情页 URL、公司名、薪资、地点、经验要求、学历、标签、job_id（从 URL 中取）；
4. 详情页：逐一点开（或直接访问详情 URL，**通过正常 page.goto 浏览，不点击任何沟通/投递按钮**），抓取 JD 正文（`.job-sec-text` 等候选选择器）、公司信息、完整标签；
5. 翻页：每关键词最多 `max_pages`(默认2) 页；
6. 拟人节奏：每个动作（goto、翻页、详情）后随机 sleep `min_delay~max_delay`（默认 3~8 秒），详情页之间再随机 2~5 秒；每轮详情页总数上限 `detail_page_limit`(默认60)；
7. 健壮性：单元素/单岗位失败用 try/except 隔离并记录；网络错误重试 2 次（tenacity 指数退避）；出现 403/验证码立即终止；
8. 所有 CSS/XPath 选择器集中放在类常量 `SELECTORS` dict 中并写注释，方便改版后一处修改。

### 6.5 内置匹配打分 skill（matcher/skill.py）⭐核心

参考开源项目 **srbhr/Resume-Matcher**（GitHub: srbhr/Resume-Matcher，Apache-2.0，28.8k star；借鉴其 FastEmbed 向量相似度与关键词覆盖方法，**不引入 Qdrant 等重依赖**）。

分三层，全部结果写库：

**第 1 层：硬过滤（规则，零成本）**
- 城市不符、学历不符、要求全职多年经验而用户找实习、命中 exclude_keywords（如「销售」「培训贷」「纯标注」「外包驻场」）、公司在黑名单 → `hard_passed=false`，直接 grade=rejected，不再调用 LLM。

**第 2 层：客观相似度（本地 FastEmbed）**
- 用 `BAAI/bge-small-zh-v1.5` 分别对简历文本、JD 文本编码，cosine 相似度 ×100 得 `embedding_score`；
- 技能覆盖率：用 LLM 结构化出的简历 skills 与从 JD 抽取的技能词（可用正则词表 + 上一轮 LLM 结果），命中率得 `skill_coverage`；
- 模型用单例懒加载，首次调用时下载并缓存到 `data/models/`。

**第 3 层：LLM 语义打分**
- 模型走 OpenAI 兼容接口，强制 JSON 输出（response_format json_object，配合 tenacity 重试解析）；
- prompt 模板放 `core/prompts.py`，打分 prompt 必须输出：
```json
{
  "hard_qualified": true,
  "hard_gaps": ["不满足的硬性条件，没有则空数组"],
  "llm_score": 0,
  "matched_points": ["简历中与JD最对口的3个点，必须引用具体项目/课程/经历"],
  "pitch_angle": "一句话，打招呼主打的切入点",
  "risk_flags": ["培训/外包/薪资异常/岗位疑似销售等风险，没有则空数组"],
  "jd_summary": "50字以内的岗位核心要求摘要"
}
```
- `final_score = round(0.35*embedding_score + 0.65*llm_score, 1)`；
- 硬条件不合格（任一层判定 false）一票否决 → rejected；
- 分档：≥75 `recommended`，50–74 `candidate`，<50 `rejected`。

### 6.6 话术生成（core/greeting.py + prompts.py）

- 输入：岗位 JD 摘要 + matched_points + pitch_angle + 结构化简历；
- 一次生成 3 条，三种风格：
  - `direct` 项目直击型：开头直接点中岗位核心需求 + 一个对口项目及可量化结果；
  - `skill` 技能匹配型：围绕技能栈重合度展开；
  - `sincere` 诚意请教型：表达对团队/业务方向的兴趣并请教；
- 硬性约束：每条 ≤280 字；必须包含简历中至少一个具体项目名/成果；禁止出现「微信」「vx」「加个」「私聊」「QQ」等站外联系方式词；三条开头不得雷同；不得出现「您好，我对贵公司岗位很感兴趣」这类空洞模板；
- 生成后做敏感词自检，命中则重新生成（最多 2 次），仍不合格则丢弃该条。

### 6.7 流水线（core/pipeline.py）

`run_once(trigger: str = "schedule")`：
1. 写一条 RunLog(state=running)，加进程内任务锁，禁止并发；
2. 加载 active 简历；无简历则终止并提示上传；
3. ensure_login → 失败/验证码 → state=paused_captcha、推送、结束；
4. 遍历 profile 中每个「关键词 × 城市」组合，调 BOSS 适配器采集，入库去重；
5. 对新岗位依次：硬过滤 → 向量分 → LLM 分 → 写 Evaluation；
6. 对 recommended/candidate 生成 3 条话术；
7. 汇总 RunLog 计数，state=success；
8. 调 notify 推送汇总；
9. finally 关闭浏览器、释放锁。

### 6.8 推送（notify/push.py）

- 飞书：POST webhook，文本/富文本消息：`JobAgent 本轮完成：新抓取 X 个｜推荐 Y｜可考虑 Z｜淘汰 N。平台地址 http://localhost:8000`；验证码暂停时单独推送告警；
- Server 酱：`https://sctapi.ftqq.com/{SENDKEY}.send`；
- 推送失败仅写日志，不影响流水线。

### 6.9 Web 平台（web/app.py + templates）

浅色简洁的企业后台风格（白底+中性灰+一个主色，不要紫色/渐变），顶部导航。页面：

1. **仪表盘 `/`**：今日新岗位数、推荐数、可考虑数、已投递数（大数字卡片）；任务状态（上次运行时间/耗时/结果、下次定时时间、当前是否运行中）；「立即运行一轮」按钮（POST 后端，pipeline 后台线程执行，按钮显示进行中）；分数分布简单条形图（纯 CSS 即可）。
2. **岗位列表 `/jobs`**：Tab 分档（推荐/可考虑/已淘汰/全部）+ 筛选（城市、状态、关键词搜索、分数区间）；按 final_score 降序。卡片内容：岗位名、公司、薪资、城市·经验·学历、匹配分彩色徽标、matched_points 前 2 条、风险标记小图标；操作区：
   - 「查看详情」→ `/jobs/{id}`；
   - 「话术 ▾」展开 3 条，每条带「复制」按钮（navigator.clipboard）；
   - 「去 BOSS 打招呼」：`<a target="_blank">` 指向岗位原始 URL；
   - 状态下拉：待处理 / 已投递 / 已约面 / 不感兴趣（AJAX 保存）。
3. **岗位详情 `/jobs/{id}`**：JD 全文、硬缺口、匹配点、风险标记、三项分数拆解、jd_summary、3 条话术（带复制）、岗位原始链接、所属运行批次。
4. **简历管理 `/resume`**：当前 active 简历（文件名、上传时间、结构化预览）、上传表单（接受 .pdf/.docx/.md，multipart）、上传后自动解析并结构化、「用最新简历重新打分全部岗位」按钮（后台执行）。
5. **设置 `/settings`**：profile.yaml 表单化编辑（关键词、城市、阈值、延迟、cron、模型名、base_url）+ secrets 文本框（api_key、飞书 webhook、Server酱 key）；「测试 LLM 连通」「测试飞书推送」按钮；显示 BOSS 登录态状态 + 「重新登录 BOSS」说明（提示运行 `python main.py login`）。
6. **运行日志 `/runs`**：RunLog 表格（时间、状态、各计数、耗时、错误信息），paused_captcha 状态显示截图缩略图。

### 6.10 入口与定时

- `main.py`：argparse 三个子命令
  - `login`：打开浏览器持久化会话登录 BOSS；
  - `serve`：启动 uvicorn（127.0.0.1:8000），启动时初始化 DB 并启动 APScheduler；
  - `run-once`：命令行跑一轮（调试用）；
- `core/scheduler.py`：BackgroundScheduler，按 profile 中 `schedule.cron`（默认 `30 9 * * *` 与 `30 14 * * *`）触发 pipeline.run_once；
- `scripts/start.ps1`（已提供）：激活 venv 并 serve；
- `scripts/install_scheduled_task.ps1`（已提供）：用 schtasks 注册「用户登录时隐藏窗口自启 start.ps1」。

---

## 七、开发顺序（严格按此顺序，每阶段自测通过再进下一阶段）

| 阶段 | 内容 | 自测标准 |
|---|---|---|
| P0 | config 加载、models 建表、FastAPI 骨架、base 模板与导航 | `python main.py serve` 能打开空仪表盘；`data/job_agent.db` 自动创建 |
| P1 | 简历上传/解析/结构化（先只做网页与命令行） | 上传一份 PDF 简历，能看到结构化 JSON；重传可切换 active |
| P2 | browser 会话 + BOSS 采集器（XHR 拦截+DOM 降级） | `python main.py login` 扫码后，`run-once` 能抓 ≥20 个真实岗位入库、无重复、全程无任何点击投递行为 |
| P3 | matcher/skill.py 三层打分 | 对已抓岗位批量打分，分数与分项落库；抽查人工判断基本合理 |
| P4 | 话术生成 | 每岗位 3 条、风格不同、含具体项目、无敏感词、≤280 字 |
| P5 | Web 全部页面（岗位列表/详情/简历/设置/日志）与状态交互 | 能完成：看分、展开复制话术、跳转 BOSS、标记状态、上传简历、手动触发 |
| P6 | APScheduler + 飞书推送 | 改 cron 为 2 分钟后验证自动触发；飞书收到汇总；验证码场景能暂停并告警 |
| P7 | 自启动脚本、README 运行说明、整体联调 | 关机重开后平台自启；完整跑一轮无报错 |

注意：P2 阶段若 BOSS 页面结构与本任务书描述不一致，以实际页面为准修改选择器，并把选择器集中到 `SELECTORS` 常量。

---

## 八、总体验收标准

1. 从零启动只需要：①复制两个 example 配置并填 key → ②`python main.py login` 扫码 → ③`python main.py serve`；
2. 每轮稳定抓取 ≥20 个岗位，跨轮不重复；
3. 抽查 20 个岗位，recommended 档与人工判断一致率 ≥80%；
4. 话术每条不同、含项目细节、无敏感词；
5. 网页可完成全部查看与交互操作；
6. 定时自动执行 + 飞书推送到账；
7. 验证码/登录失效时安全暂停、有通知、不崩溃；
8. 全代码库中不存在任何点击「沟通/投递/收藏/发送」的逻辑。

---

## 九、编码规范

- 所有中文注释与文档；函数有 docstring；关键业务分支写注释；
- 配置项不允许硬编码（城市码、选择器、阈值、URL 都进配置或类常量）；
- 异常不允许裸 `except: pass`，至少 loguru 记录；
- LLM 调用统一封装：超时、重试（tenacity）、JSON 解析失败重试、token 用量记录到日志；
- 密钥只从 secrets.yaml 读，禁止写死；
- 前端不用任何需要 npm build 的框架。
