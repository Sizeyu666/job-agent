# JobAgent 项目规则（TRAE 自动加载，每次开发都必须遵守）

## 项目性质
本地「求职副驾驶」Web 平台：自动采集 BOSS 直聘岗位 → 简历匹配打分 → 生成打招呼话术 → 网页展示并提供跳转链接。**开发任务总说明见根目录 `TASKS.md`，开工前必须先完整阅读，严格按其中 P0–P7 顺序实现。**

## 不可逾越的红线
1. 只允许浏览/采集，**严禁**编写任何点击「立即沟通」「投递」「收藏」「发送」、提交表单、发送消息的代码；
2. 严禁绕过验证码/滑块/风控；检测到验证码必须截图、暂停、推送通知；
3. 严禁把 API Key、webhook、简历明文写进代码或提交到 git。

## 技术栈（已锁定，不得自行更换）
- Python 3.11，虚拟环境在 `.venv/`，运行用 `.venv\Scripts\python.exe`；
- FastAPI + Jinja2 + Tailwind CDN + Alpine.js CDN，**零前端构建，禁止引入 npm/vite/react 工程**；
- SQLite + SQLModel；Playwright（persistent context，非 headless）；
- FastEmbed `BAAI/bge-small-zh-v1.5` 做本地向量匹配；LLM 走 OpenAI 兼容接口（openai SDK）；
- APScheduler 内嵌调度；loguru 日志；飞书 webhook 推送。

## 代码约定
- 注释与文档用中文；公开函数写 docstring；
- 所有平台选择器集中到适配器类的 `SELECTORS` 常量，便于页改版后一处维护；
- 单个岗位/单条请求的异常必须隔离，不得中断整轮任务；
- LLM 调用必须封装：超时、tenacity 重试、JSON 解析失败重试；
- 运行时数据（data/、logs/、resumes/、output/、secrets.yaml、.venv/）不入库。

## 每阶段完成后
- 对照 TASKS.md 第七节的自测标准实际运行验证，不允许只写代码不运行；
- 出现 BOSS 页面结构与任务书不符时，以实际页面为准并注释说明。
