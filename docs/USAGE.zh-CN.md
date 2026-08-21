# BTAP 使用指南

[English](USAGE.md) | 中文

本文档定义 `browsertap-mcp` 在现有 Chrome、Edge 或 Opera 会话中的推荐操作方式，
目标是在保持任务可控的同时，尽量避免改变用户正在使用的浏览器和桌面状态。55 个工具及其
参数以根目录的 [README 中文版](../README.zh-CN.md)为权威参考；本文档仅说明操作流程和
边界选择。

## 1. 操作层级

BTAP 的操作分为三个层级：

| 层级 | 操作对象 | 对可见界面的影响 |
|---|---|---|
| 后台标签页操作 | 通过 CDP 或扩展操作指定标签页 | 不改变。`switch_tab` 仅修改后续调用目标 |
| 前台标签页操作 | 可见标签页及其浏览器窗口 | 会改变。仅由 `activate_tab` 或 `switch_tab(activate=true)` 显式触发 |
| 桌面级操作 | 操作系统屏幕、真实鼠标和键盘 | 会改变。物理输入作用于屏幕上实际可见的对象 |

默认采用后台标签页操作。`switch_tab` 选定标签页后，该标签页不会自动变为可见、获得窗口
焦点或成为浏览器当前活动页。

## 2. 只读操作

只读检查建议采用以下流程：

1. 调用 `list_tabs` 并记录初始标签页集合。任务开始前已存在的标签页记为用户标签页（`U`），
   默认不得关闭或导航。
2. 仅在只读或轻量操作中使用匹配的用户标签页，并保存其完整 `session_id`。
3. 后续调用均显式传入该 `session_id`。并行任务共享默认目标，因此不应依赖隐式会话选择。
4. 优先使用 `scan_page`、`execute_js`、`wait_for`、`scroll_page` 和 `page_*`。这些工具不会
   移动桌面光标，也不会将浏览器窗口切换到前台。
5. 需要指定标签页的像素信息时，使用 `capture_page_screenshot`。

典型调用顺序：

```text
list_tabs()
scan_page(session_id="chrome_client:123")
wait_for(selector="main", session_id="chrome_client:123")
capture_page_screenshot(session_id="chrome_client:123", full_page=true)
```

需要导航、填写表单或执行其他明显状态变更时，应创建 Agent 自有标签页，不应修改用户标签页。

## 3. 状态变更与标签页所有权

导航、表单、下载等状态变更操作通常使用 `open_new_tab` 创建工作标签页。新标签页默认在后台
打开。调用方应保存以下返回值：

- `session_id`：后续页面操作的明确目标；
- `generation`：原生标签页的生命周期标识，用于防止复用过期 ID；
- `owner_id`：执行安全清理时使用的所有权凭据。

操作期间应保持同一 `session_id`，并在可行时持续使用后台方式。任务结束后，只关闭本任务
创建且 `owner_id` 匹配的标签页。若用户已提前关闭该标签页，清理结果为 `already_gone`；不得
为完成清理而重新创建标签页或复用旧标签页 ID。

## 4. 截图来源与模型能力

两个截图工具具有不同的数据来源：

- `capture_page_screenshot` 通过 CDP 捕获指定标签页，支持后台标签页、整页截图和显式裁剪，
  无需将页面切换到前台。MCP 结果包含图片内容，并可返回相关元数据或 base64。
- `capture_desktop_screenshot` 捕获操作系统当前可见的虚拟桌面，覆盖全部显示器，并保留可能
  出现的负显示器坐标。该工具适用于核对物理输入、浏览器界面、原生对话框和文件选择器，不
  捕获后台标签页。浏览器最小化或被其他窗口遮挡时，截图就是当时实际显示的内容。返回结果
  包含 `monitor_count`、`left`、`top` 和说明该边界的 `model_note`。

工具成功返回图片附件或保存路径，仅表示截图已生成，不表示当前模型或宿主具备像素读取能力。
需要判断布局、canvas、WebGL、验证码或其他视觉状态时，应使用支持图片输入的多模态模型。
模型不支持图片输入时，应改用 `scan_page`、`execute_js`、页面数据 API 或环境提供的 OCR。
对于终端模拟器、canvas 和 WebGL 页面，仍应优先获取结构化数据，仅在确需像素判断时使用截图。

## 5. 前台激活与物理输入

普通表单、页面按钮、页内快捷键、滚动和拖拽应优先使用 `page_*` 或其他 CDP 工具。以下情况
才需要前台激活或操作系统级输入：

- 浏览器自身界面；
- 扩展弹窗；
- 原生文件选择器；
- 操作系统对话框；
- 页面不存在可用的 DOM、协议或 API 操作入口。

物理输入的执行顺序如下：

1. 仅在用户需要查看页面或桌面操作依赖可见状态时，显式激活目标标签页。
2. BTAP 验证目标窗口、标签页所有权和 `on_screen` 状态。
3. BTAP 获取跨进程输入锁并等待安静窗口；若检测到用户鼠标或键盘活动，本次操作取消。
4. 无法确认目标显示在屏幕上时，返回 `activation_failed`，且不发送输入。

默认 `lab` profile 免 elicitation，以支持连续自动化。设置
`BROWSERTAP_LAB_NO_ELICIT=0` 或 `false` 可恢复 lab 会话级询问；`safe` profile 对每次
物理输入和站点 `allow` 操作进行询问。两种 profile 均保留输入锁、安静窗口、所有权检查、
目标激活和屏幕确认。

## 6. 对话框、权限与挑战页

- 导航结果会受 JavaScript dialog 或 `beforeunload` 影响时，应显式选择 `dismiss`、`accept`
  或 `manual`。
- 站点权限采用短期租约。`set_site_permission` 记录并恢复原设置；需要立即恢复时调用
  `reset_site_permissions`。
- Turnstile 等挑战无进展时返回 `challenge_stalled`。后续人工处理应继续使用同一个标签页，
  不应启动另一个浏览器会话。
- BTAP 不会自动降级到 Playwright、无头浏览器或其他浏览器 profile，以确保登录态和前台影响
  边界保持一致。

## 7. 诊断与升级

基础诊断命令：

```text
browsertap doctor
```

`get_setup_status` 用于核对 package、bridge、extension 和 protocol 版本。Bridge 是独立后台
进程：允许自动拉起时，未监听的 bridge 会自动启动；仍占用端口的旧 bridge 必须执行
`browsertap bridge --restart`，该操作不会改变浏览器前台状态。未打包扩展的源文件发生
变化后，仍需在 `chrome://extensions` 或 Edge、Opera 对应页面中手动执行 **Reload**。工具
schema 变化后，需重启 MCP 会话或客户端以重新读取工具描述。

若将 `BROWSERTAP_BRIDGE_PORT` 从 `18765` 改为其他值，还需在扩展的 Service Worker 控制台
告知一次相同的 WebSocket 端口（见[故障排查](TROUBLESHOOTING.zh-CN.md)）。Python 环境变量
无法直接修改已安装扩展的 storage；两端端口不一致时不会建立连接。

完整恢复流程见[故障排查](TROUBLESHOOTING.zh-CN.md)。`/link` HTTP 通道使用
`~/.browsertap/bridge-token`。该 token、浏览器 profile、Cookies、含个人信息的截图和
本地日志均不得提交到 Git。

## 8. 提示词示例

以下提示词用于明确后台操作和标签页所有权边界：

```text
列出已连接的标签页，不要导航或关闭已有标签页。在后台检查匹配页面，后续调用均显式使用
同一个 session_id。
```

```text
为该表单创建一个后台标签页，保存返回的 session_id、generation 和 owner_id。优先使用
页面级工具，任务结束后仅关闭本任务拥有的标签页。
```

```text
检查指定 session 的视觉布局，但不切换前台。使用 capture_page_screenshot；仅在需要查看
实际显示器或原生对话框时使用 capture_desktop_screenshot。
```

## 9. 安全边界

BTAP 控制用户授权给 MCP 客户端的真实浏览器 profile。页面内容属于不可信输入，可能包含
prompt injection。BTAP 本身不是安全隔离边界，应仅连接适合由该 MCP 客户端访问的账号和会话。
威胁模型及漏洞报告方式见 [SECURITY.md](../SECURITY.md)。
