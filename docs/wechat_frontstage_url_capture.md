# 微信前台短接管获取公众号文章 URL 方法论

本文记录一套在搜狗/网页搜索不稳定时，通过 Mac 微信客户端获取公众号文章 URL 的半自动方案。

适用场景：

- 搜狗微信搜索的时间筛选 URL 不可用，或自动化会话被重定向/反爬。
- 目标公众号在 Mac 微信里可以正常搜索、进入主页、看到文章列表。
- 需要抓取当天或最近文章的 URL，再交给后台抓取器处理正文。

不适用场景：

- 希望微信在完全后台无人感知地被点击。Mac 微信没有暴露足够稳定的 DOM/Accessibility 控件，纯 UI 点击通常必须前台运行。
- 公众号主页或文章页在用户手动打开时也持续 403。这时应优先排查网络、代理、微信缓存或登录态。

## 核心结论

推荐模式是：

1. 前台短接管微信，只负责搜索公众号、进入主页、筛选日期分组、打开文章、复制 URL。
2. 一旦拿到 `mp.weixin.qq.com` URL，立即释放电脑控制权。
3. 后台用代码抓取、清洗、去重、入库和进入 pipeline。

这样把必须依赖微信前台状态的部分压缩到几十秒，把耗时处理留给后台。

## 操作协议

为了避免鼠标键盘焦点冲突，每次接管前后都要明确同步：

- 接管前：`现在开始接管，大约 N 秒，请先别动鼠标键盘。`
- 接管后：`这轮接管结束，你可以动电脑。`

用户在接管期间不要切窗口、移动鼠标、敲键盘。因为当前方案使用 macOS 全局鼠标/键盘事件，前台是谁就会操作谁。

## 已验证路径

以“量子位”为例，验证通过的路径是：

1. 将微信拉到前台。
2. 点击左上搜索框。
3. 输入公众号名，例如 `量子位`。
4. 在搜索结果里点击公众号结果，进入公众号推送窗口。
5. 点击推送窗口右上角的人像/账号按钮，进入公众号完整主页。
   这一步是必须的：推送窗口只展示一次群发里的卡片，完整主页才会展示当天全部文章。
6. 在完整主页 OCR/截图识别日期分组，例如 `今天`、`昨天`。
7. 点击目标日期分组下的文章。
8. 文章正文页打开后，点击右上角 `...` 菜单，再点击 `复制链接`。
   不使用 `Cmd+L` 或默认浏览器打开；剪贴板必须匹配 `https://mp.weixin.qq.com/...` 才算成功。
9. 关闭当前文章标签/窗口，必要时关闭露出的搜一搜旧标签，回到公众号完整主页继续采集下一篇。
10. 当前公众号采集结束后，默认关闭公众号窗口，回到微信主窗口。
11. 将 URL 交给后台抓取器。

验证通过的文章示例：

- `Ai学习的老章 / 2026-06-07`：完整主页下两篇文章均成功复制 URL。
- `ChallengeHub / 2026-06-04`：指定日期文章成功复制 URL。
- `量子位 / 2026-06-07`：完整主页下三篇文章连续成功复制 URL。

## 技术要点

### 1. 截图和 OCR 可用

Mac 微信界面可以被 `screencapture` 截到，macOS Vision OCR 可以识别微信文本，包括：

- 搜索框文字
- 公众号名称
- 日期分组
- 文章标题
- 发布时间
- 阅读数、赞数等辅助信息

这说明不需要依赖微信 DOM，也不需要 tesseract。

### 2. Accessibility 不足

System Events/Accessibility 只能看到微信顶层窗口、菜单等粗粒度元素，无法稳定读取公众号主页或文章列表里的具体控件。

因此当前可行路线是：

- 截图定位
- OCR 识别
- 坐标点击
- 截图校验

### 3. 注意 Retina 坐标缩放

`screencapture` 输出的是像素坐标，macOS 鼠标事件使用的是屏幕点坐标。在 Retina 屏幕上常见比例是 2:1。

实践中不能直接把截图像素坐标用于点击，否则可能误点到聊天输入框或其他区域。

应使用屏幕点坐标：

- 如果截图宽度是 `2560`，屏幕逻辑宽度是 `1280`，点击坐标约等于截图像素坐标除以 2。
- 每次点击前最好先截图确认目标位置。
- 第一次校准时只点击搜索框，不输入内容；确认搜索框高亮/出现光标后再粘贴。

### 4. 输入中文用剪贴板粘贴

不要依赖当前中文输入法状态。推荐：

1. 设置剪贴板为目标公众号名。
2. 点击搜索框。
3. `Cmd+V` 粘贴。

### 5. 每一步都截图校验

建议形成“动作 -> 截图 -> OCR/人工确认 -> 下一步”的节奏：

- 搜索框是否聚焦。
- 搜索词是否进入搜索框，而不是聊天输入框。
- 是否进入正确公众号主页。
- 是否看到了目标日期分组。
- 是否打开了正文页，而不是 403 页面。

## URL 获取方式

文章正文页打开后，使用微信文章菜单复制 URL：

1. 点击文章窗口右上角 `...`。
2. 在菜单中点击 `复制链接`。
3. 读取剪贴板。
4. 校验剪贴板中存在 `https://mp.weixin.qq.com/...`。

不要使用地址栏 `Cmd+A/Cmd+C` 作为主路径。Mac 微信文章页的标签栏不稳定暴露真实 URL，且容易受到旧搜一搜标签影响；菜单里的 `复制链接` 是当前验证通过的稳定路径。

拿到 URL 后，后续应切换到后台代码处理：

- 解析 `mp.weixin.qq.com` URL。
- 抓取 HTML。
- 提取标题、发布时间、作者、正文。
- 与当天已抓取 URL 去重。
- 写入 `reports/` 或 `states/`。

## Pipeline 集成

当前 pipeline 的微信公众号 URL 发现入口已经改为：

- `src/core/agent.py::fetch_wechat_foreground`
- `src/infrastructure/wechat_foreground_collector.py::collect_wechat_article_urls`

旧搜狗/浏览器搜索不再作为微信公众号来源的回退路径。`browser_fetcher.py` 仍负责微信正文抓取，并保留遗留浏览器工具用于诊断。

可用环境变量：

- `WECHAT_FOREGROUND_MAX_ARTICLES`：每个公众号最多采集几篇文章 URL，默认 `3`。
- `WECHAT_FOREGROUND_ASSUME_READY=1`：跳过命令行确认提示，适合已确认前台接管窗口的自动运行。
- `WECHAT_FOREGROUND_KEEP_ACCOUNT_WINDOW=1`：采集结束后不自动 `Cmd+W` 关闭公众号窗口；默认会关闭，以便下一个公众号从微信主窗口重新搜索。

### Windows 前台接管

Windows 现在也走同一个入口：`collect_wechat_article_urls()` 会按平台分发，macOS 使用原来的 `screencapture + Vision OCR + AppleScript/Swift`，Windows 使用 `Win32 + Microsoft UI Automation + PowerShell/.NET`。

Windows 路径默认只把微信窗口置前，不主动移动到 Codex 所在屏幕，也不强制拉伸窗口。Windows 微信对自动最大化、系统最大化消息和 `MoveWindow` 比较敏感，可能触发白屏；因此默认使用窗口内固定坐标消息打开搜索框，不再依赖全屏/最大化。

定位时会优先读取 UI Automation 暴露的可见文本来定位搜索框、公众号、文章标题和“复制链接”。如果微信版本没有暴露某个控件文本，则使用窗口相对坐标回退。

可调环境变量：

- `WECHAT_WINDOWS_PROCESS`：Windows 微信进程名，默认 `WeChat,Weixin,微信`。
- `WECHAT_WINDOWS_ENABLE_DEEPLINK`：是否允许在前台不可用时用 `weixin://` 深链唤起搜索页；可能抢前台，默认关闭。
- `WECHAT_WINDOWS_STANDARDIZE_MODE`：是否自动标准化微信窗口，默认 `none`；默认只置前，不最大化、不移动窗口。
- `WECHAT_WINDOWS_FOREGROUND_STRATEGY`：设为 `visual` 时启用 Windows 截图优先的前台采集路径；该路径会在搜索、打开公众号、打开文章、复制链接等关键步骤前准备微信窗口。
- `WECHAT_WINDOWS_VISUAL_MAXIMIZE_EACH_STEP`：`visual` 策略下是否在每个关键步骤前点击真实可见的微信最大化按钮，默认 `1`。
- `WECHAT_WINDOWS_ALLOW_VISUAL_MAXIMIZE`：设为 `1` 时才允许尝试点击微信标题栏最大化按钮；默认关闭，避免微信白屏。
- `WECHAT_WINDOWS_ALLOW_UNSAFE_RESIZE`：设为 `1` 时才允许 `maximize/workarea` 这类 API/MoveWindow 实验；默认关闭，避免微信白屏。
- `WECHAT_WINDOWS_SKIP_STANDARDIZE=1`：完全跳过窗口标准化，复用用户手动摆好的微信窗口。
- `WECHAT_WINDOWS_SEARCH_X/Y`：搜索框坐标回退，默认 `0.19` / `0.065`。
- `WECHAT_WINDOWS_RESULT_X/Y`：搜索结果坐标回退，默认 `0.20` / `0.235`。
- `WECHAT_WINDOWS_HOME_POINTS`：进入公众号完整主页的候选点击点，格式 `x,y;x,y`。
- `WECHAT_WINDOWS_ARTICLE_POINTS`：文章卡片候选点击点，格式 `x,y;x,y`。
- `WECHAT_WINDOWS_MORE_POINTS` / `WECHAT_WINDOWS_COPY_POINTS`：文章页菜单和“复制链接”候选点击点。

Windows 诊断截图默认写入 `%TEMP%\wechat_foreground_debug`。如果真机运行时某一步点偏，优先查看对应阶段截图，然后微调上面的窗口相对坐标。

真实接管前建议先跑无点击预检：

```bash
python -m src.infrastructure.wechat_foreground_collector --diagnose-windows --no-capture
```

判断方式：

- `processes` 非空：已找到微信进程。
- `visible_top_windows > 0`：已找到可接管的微信主窗口，可以继续真实采集。
- `visible_top_windows = 0`：微信在后台或托盘里，但没有打开可见主窗口；先手动打开 Windows 微信主窗口并确认已登录。
- `uia_text_count > 0`：Windows 能读取微信暴露的可见文本，后续定位会优先走 UI Automation；否则会更多依赖相对坐标回退。

如果诊断截图是黑屏，说明当前执行上下文拿不到交互桌面画面。此时 Windows 入口会自动尝试本机微信缓存 fallback：扫描 `AppData\Roaming\Tencent\xwechat` / `WeChat` 下的 Chromium/LevelDB 缓存，提取 `mp.weixin.qq.com/s?...`，并根据缓存上下文中的 `brandName` / `title` 匹配当前公众号名。

Windows 路径默认不会在缓存 fallback 前自动唤起 `weixin://resourceid/Search/app.html?...`，以免用户正在使用电脑时被抢前台。只有交互确认过接管，或显式设置 `WECHAT_WINDOWS_ENABLE_DEEPLINK=1` 后，才会尝试用深链把微信从托盘/隐藏态拉回目标公众号搜索页；如果仍然黑屏或没有可见窗口，则继续走缓存。

可以单独验证缓存 fallback：

```bash
python -m src.infrastructure.wechat_foreground_collector --scan-windows-cache ChallengeHub --max-articles 3
```

缓存 fallback 默认只返回匹配公众号名的结果。若需要临时返回最近缓存文章，可设置 `WECHAT_WINDOWS_CACHE_ALLOW_RECENT=1`，但这可能混入其他公众号文章，生产运行不建议默认开启。

对于缓存里没有 `brandName` / `title`，但能通过 `__biz` 确认公众号的旧记录，可以设置 `WECHAT_WINDOWS_CACHE_ALLOW_STALE_BIZ=1` 启用长历史扫描，扫描天数由 `WECHAT_WINDOWS_CACHE_BIZ_MAX_AGE_DAYS` 控制，默认 `1095`。这类结果可能不是当天文章，只适合诊断或应急。

## 403 排查

如果文章列表能打开，但文章详情显示 `HTTP ERROR 403`：

1. 用户手动点同一篇文章验证。
2. 如果用户手动也 403，说明不是自动化点击问题。
3. 优先排查：
   - Clash/VPN/系统代理。
   - 微信 Mac 内置浏览器缓存。
   - 微信登录态。
   - 是否需要重启微信。
4. 如果用户手动可以打开，自动化再重试，通常是前台状态或加载时机问题。

## 后台运行边界

当前微信 UI 自动化不能可靠做到真正后台点击，原因：

- macOS 全局鼠标/键盘事件只作用于当前前台焦点。
- 微信 Mac 未暴露足够稳定的 Accessibility 子控件。
- 微信内置网页不是普通浏览器 DOM，不能像 Selenium/Playwright 那样后台驱动。

可优化方向：

- 缩短前台接管时间，只获取 URL。
- URL 获取后立即释放前台，把正文抓取放到后台。
- 使用独立 Mac、备用用户会话、远程桌面或虚拟机专门跑微信。
- 将截图/OCR/点击流程脚本化，但仍按“接管窗口”运行。

## 推荐实现拆分

可以将后续实现拆成两个模块：

1. `wechat_foreground_collector`
   - 前台操作微信。
   - 按公众号名搜索并进入主页。
   - OCR 识别日期分组和文章标题。
   - 打开目标文章并复制 URL。
   - 输出 URL 列表。

2. `wechat_article_fetcher`
   - 后台处理 URL。
   - 抓取正文和元信息。
   - 清洗文本。
   - 去重并写入 pipeline。

这样即使前台采集需要人工短暂停顿，后续主体流程仍然可以无人值守。

## 最小人工协作流程

每次 pipeline 运行一轮：

1. 用户回复：`可以接管`
2. 自动化接管微信前台，连续处理配置里的公众号。
3. 对每个公众号搜索、进入完整主页、按日期采集文章 URL。
4. 全部公众号 URL 采集结束后释放控制权。
5. 后台抓取正文、清洗、去重、写入 pipeline。

这比长时间占用整台电脑更可接受，也避免每个公众号都重复要求用户按 Enter。
