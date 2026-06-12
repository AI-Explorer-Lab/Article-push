# 微信前台短接管获取公众号文章 URL 方法论

本文记录一套在搜狗/网页搜索不稳定时，通过本机微信客户端获取公众号文章 URL 的半自动方案。macOS 和 Windows 使用同一个入口函数，但前台接管策略不同：macOS 走系统截图/OCR/AppleScript/Swift，Windows 走截图优先的 Computer Use 视觉路径，默认禁用可能导致白屏的 Win32 强制窗口操作。

适用场景：

- 搜狗微信搜索的时间筛选 URL 不可用，或自动化会话被重定向/反爬。
- 目标公众号在本机微信里可以正常搜索、进入主页、看到文章列表。
- 需要抓取当天或最近文章的 URL，再交给后台抓取器处理正文。

不适用场景：

- 希望微信在完全后台无人感知地被点击。微信桌面端没有暴露足够稳定的 DOM/Accessibility 控件，纯 UI 点击通常必须前台运行。
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

用户在接管期间不要切窗口、移动鼠标、敲键盘。因为当前方案使用系统级鼠标/键盘事件，前台是谁就会操作谁。Windows 上还要先截图确认操作对象确实是微信窗口，而不是 Codex 窗口、空白层或被遮挡的窗口。

## 已验证路径：macOS

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

历史验证通过的文章示例：

- `Ai学习的老章 / 2026-06-07`：完整主页下两篇文章均成功复制 URL。
- `ChallengeHub / 2026-06-04`：指定日期文章成功复制 URL。
- `量子位 / 2026-06-07`：完整主页下三篇文章连续成功复制 URL。

## 已验证路径：Windows

Windows 不要从 Win32 API 最大化/移动微信开始。实测 `ShowWindow(SW_MAXIMIZE)`、`WM_SYSCOMMAND/SC_MAXIMIZE`、`MoveWindow` 等系统窗口操作容易让 Windows 微信变成白屏。当前验证通过的路径是 Computer Use 截图优先：

1. `list_apps` 找到 `Weixin.exe` / `WeChatAppEx` 对应的微信窗口。
2. `activate_window` 后立即截图，确认截图里是真实微信界面；如果看到白屏、Codex 或错误屏幕，停止。
3. 点击微信标题栏真实可见的最大化按钮，让微信占满当前测试屏幕。
4. 再截图确认窗口已最大化，后续坐标都基于当前微信窗口尺寸计算。
5. 点击搜索框，粘贴公众号名，例如 `QbitAI`、`ChallengeHub`、`老章`。
6. 点击公众号搜索结果，进入公众号浮层或主页。
7. 把公众号浮层/文章页当作当前可见目标窗口处理，不要隔着主窗口猜坐标。
8. 点击目标日期下的文章卡片；文章页打开后点击右上 `...`，再点 `复制链接`。
9. 读取剪贴板并校验必须是 `https://mp.weixin.qq.com/...`。
10. 回到公众号浮层，继续点击同一天其它可见文章卡片。

2026-06-09 Windows 实测结果：

- `QbitAI / 2026-06-08 18:59`：`你天天刷的小红书，正在长出一个GitHub`，URL `https://mp.weixin.qq.com/s/iccrl0Dz9zv5ql9Yq31D1g`。
- `老章 / 2026-06-08 13:07`：`中小企业必备AI数字员工，已开源，Claude Code/Codex 一句话安装，陆续更新中`，URL `https://mp.weixin.qq.com/s/atu_cQ3_N6zV_Ub98TX-1g`。
- `ChallengeHub / 2026-06-08 16:12`：`RAG 还在说“我信息不够”？谷歌Gemini这套 Agentic RAG 直接逼它接着搜`，URL `https://mp.weixin.qq.com/s/xsXViSm7r2fEgbidwRIqbg`。
- `QbitAI` 同一天同一卡片内 4 篇可见文章均已测试，分别复制到 `iccrl0Dz9zv5ql9Yq31D1g`、`MoM0hjYiOBaX6UdUOReYqw`、`Ftus2OvqbeYLWVVFS2Ke_g`、`YPK0Uiwi18JKz-ZEDsngFA`。

尚未完全验证：如果同一天文章多到需要在公众号浮层内部滚动，滚动、定位下一批卡片和继续复制 URL 还需要单独做视觉校准。

## 技术要点

### 1. 截图和 OCR/视觉确认可用

Mac 微信界面可以被 `screencapture` 截到，macOS Vision OCR 可以识别微信文本，包括：

- 搜索框文字
- 公众号名称
- 日期分组
- 文章标题
- 发布时间
- 阅读数、赞数等辅助信息

Windows 在 Codex 环境中更适合通过 Computer Use 截图直接确认界面状态：先确认微信已经可见且不是白屏，再做点击。Python 里的 Win32/UIA 诊断可以辅助发现窗口和缓存 URL，但不能替代真实截图校验。

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

- `WECHAT_FOREGROUND_MAX_ARTICLES`：每个公众号最多采集几篇文章 URL，默认 `10`，用于覆盖同一天多篇推文。
- `WECHAT_FOREGROUND_REQUIRE_DATE_VERIFY=1`：复制到文章 URL 后，必须解析到文章真实发布日期且落在目标日期窗口内才收录；设为 `0` 才允许发布日期未知的 URL 通过。
- `WECHAT_FOREGROUND_ASSUME_READY=1`：跳过命令行确认提示，适合已确认前台接管窗口的自动运行。
- `WECHAT_FOREGROUND_KEEP_ACCOUNT_WINDOW=1`：采集结束后不自动 `Cmd+W` 关闭公众号窗口；默认会关闭，以便下一个公众号从微信主窗口重新搜索。

### Windows 前台接管

Windows 现在也走同一个入口：`collect_wechat_article_urls()` 会按平台分发，macOS 使用 `screencapture + Vision OCR + AppleScript/Swift`，Windows 默认采用安全保守策略：没有显式允许前台视觉接管时，不做真实点击，优先诊断窗口状态和扫描微信缓存 fallback。

如果要在 Codex 里真实操作 Windows 微信，推荐显式设置 `WECHAT_WINDOWS_FOREGROUND_STRATEGY=visual`。当前稳定路径不会默认启动新的微信实例；它会先恢复已有微信主窗口并截图预检，确认前台是微信且不是白屏/空壳窗口。进入微信后的第一步是检查是否最大化，不是最大化就只点击微信真实可见的最大化按钮，最大化和非白屏确认通过后，才按窗口相对位置搜索、打开公众号、打开文章和复制链接。文章窗口返回公众号页时会关闭已记录的文章窗口句柄，不发送全局 `Ctrl+W`，避免误关 Codex/VS Code 或其它前台程序。

不要把 `maximize/workarea/MoveWindow` 当成生产策略。它们保留在代码里只是为了手动实验和诊断，默认被保护开关拦住。

可调环境变量：

- `WECHAT_WINDOWS_PROCESS`：Windows 微信进程名，默认 `Weixin,WeChat,微信,WeChatAppEx`。
- `WECHAT_WINDOWS_ENABLE_DEEPLINK`：是否允许在前台不可用时用 `weixin://` 深链唤起搜索页；可能抢前台，默认关闭。
- `WECHAT_WINDOWS_STANDARDIZE_MODE`：旧式窗口标准化模式，默认 `none`；不建议在生产里改成 `maximize` 或 `workarea`。
- `WECHAT_WINDOWS_FOREGROUND_STRATEGY`：设为 `visual` 时启用 Windows 截图优先的前台采集路径；该路径会在搜索、打开公众号、打开文章、复制链接等关键步骤前确认微信仍在前台且可渲染。
- `WECHAT_WINDOWS_AUTO_REVEAL`：`visual` 预检失败时是否自动恢复已有微信主窗口，默认 `1`；它只操作已有窗口句柄，不创建新微信实例。
- `WECHAT_WINDOWS_AUTO_REVEAL_USE_DEEPLINK`：自动恢复后是否再使用 `weixin://` 深链，默认关闭，避免新建登录壳。
- `WECHAT_WINDOWS_AUTO_REVEAL_ALLOW_LAUNCH`：自动恢复失败时是否允许启动微信 exe，默认关闭，避免新建未登录实例。
- `WECHAT_WINDOWS_VISUAL_MAXIMIZE_EACH_STEP`：`visual` 策略下是否在每个关键步骤前再次检查最大化，默认 `0`；开始搜索前会先强制确认已有微信窗口已最大化。
- `WECHAT_WINDOWS_REQUIRE_MAXIMIZED`：`visual` 策略下是否要求搜索前先最大化微信，默认 `1`；设为 `0` 才跳过。
- `WECHAT_WINDOWS_AFTER_AUTO_REVEAL_WAIT`：自动恢复已有微信窗口并最大化后的短等待秒数，默认 `0.7`。
- `WECHAT_WINDOWS_AFTER_RESTORE_WAIT`：自动恢复已有微信窗口后的短等待秒数，默认 `0.45`。
- `WECHAT_WINDOWS_AFTER_INITIAL_MAXIMIZE_WAIT`：开始搜索前点击真实最大化按钮后的短等待秒数，默认 `0.35`。
- `WECHAT_WINDOWS_REQUIRE_VISUAL_PREFLIGHT`：`visual` 策略下正式点击前是否必须通过无点击预检，默认 `1`；预检失败时会拒绝接管，避免微信白屏或误关其它窗口。
- `WECHAT_WINDOWS_ALLOW_FOREGROUND_CLICKS`：兼容旧实现的手动开关；不推荐作为主路径，优先使用 `WECHAT_WINDOWS_FOREGROUND_STRATEGY=visual`。
- `WECHAT_WINDOWS_ALLOW_VISUAL_MAXIMIZE`：旧 Python/Win32 视觉最大化开关；当前真机实测不如 Computer Use 真实点击稳定。
- `WECHAT_WINDOWS_ALLOW_UNSAFE_RESIZE`：设为 `1` 时才允许 `maximize/workarea` 这类 API/MoveWindow 实验；默认关闭，避免微信白屏。
- `WECHAT_WINDOWS_SKIP_STANDARDIZE=1`：完全跳过窗口标准化，复用用户手动摆好的微信窗口。
- `WECHAT_WINDOWS_SEARCH_X/Y`：搜索框坐标回退，默认 `0.19` / `0.065`。
- `WECHAT_WINDOWS_RESULT_X/Y`：搜索结果坐标回退，默认 `0.20` / `0.235`。
- `WECHAT_WINDOWS_HOME_POINTS`：进入公众号完整主页的候选点击点，格式 `x,y;x,y`。
- `WECHAT_WINDOWS_ARTICLE_POINTS`：文章卡片候选点击点，格式 `x,y;x,y`。
- `WECHAT_WINDOWS_MORE_POINTS` / `WECHAT_WINDOWS_COPY_POINTS`：文章页菜单和“复制链接”候选点击点。

Windows 诊断截图默认写入 `%TEMP%\wechat_foreground_debug`。如果真机运行时某一步点偏，优先查看对应阶段截图，然后微调上面的窗口相对坐标。若截图不是微信、前台进程不是微信，或画面是白屏/只有 `Weixin` 壳标题，不要继续点击，先让用户恢复并切到微信窗口。

真实接管前建议先跑无点击预检。正式 `visual` 采集会强制使用带截图预检；如果当前前台是 Codex/VS Code/其它窗口，会返回 `foreground_not_wechat` 并停止：

```bash
python -m src.infrastructure.wechat_foreground_collector --diagnose-windows --no-capture
python -m src.infrastructure.wechat_foreground_collector --preflight-windows-visual
```

判断方式：

- `processes` 非空：已找到微信进程。
- `visible_top_windows > 0`：已找到微信主窗口；还需要 `foreground_is_wechat=true` 才允许真实采集。
- `foreground_blocker = foreground_not_wechat`：当前前台不是微信；请先把 Windows 微信主窗口切到前台。
- `uia_content_text_count > 0`：微信窗口暴露了除 `Weixin`/`WeChat` 壳标题之外的真实内容；只有壳标题会被当成不可渲染。
- `visible_top_windows = 0`：微信在后台或托盘里，但没有打开可见主窗口；先手动打开 Windows 微信主窗口并确认已登录。
- `uia_text_count > 0`：Windows 能读取微信暴露的可见文本，可辅助定位；但最终是否继续点击仍以截图是否可见、是否不是白屏为准。

如果诊断截图是黑屏，说明当前执行上下文拿不到交互桌面画面。此时 Windows 入口会自动尝试本机微信缓存 fallback：扫描 `AppData\Roaming\Tencent\xwechat` / `WeChat` 下的 Chromium/LevelDB 缓存，提取 `mp.weixin.qq.com/s?...`，并根据缓存上下文中的 `brandName` / `title` 匹配当前公众号名。

Windows 路径默认不会在缓存 fallback 前自动唤起 `weixin://resourceid/Search/app.html?...`，以免用户正在使用电脑时被抢前台。只有交互确认过接管，或显式设置 `WECHAT_WINDOWS_ENABLE_DEEPLINK=1` 后，才会尝试用深链把微信从托盘/隐藏态拉回目标公众号搜索页；如果仍然黑屏或没有可见窗口，则继续走缓存。

可以单独验证缓存 fallback：

```bash
python -m src.infrastructure.wechat_foreground_collector --scan-windows-cache ChallengeHub --target-date 2026-06-09 --days 1 --max-articles 10
python -m unittest tests.test_wechat_foreground_collector -v
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
