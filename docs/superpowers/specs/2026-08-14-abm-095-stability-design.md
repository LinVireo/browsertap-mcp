# ABM 0.3.0 稳定性与 95 分交付设计

日期：2026-08-14

## 目标

本阶段采用“稳定面强化”路线，冻结现有 55 个 MCP 工具，不新增或删除工具。
交付目标是：

1. 55 个工具全部进入机器可校验的测试覆盖矩阵。
2. 可安全操作真实浏览器的工具具备 live Chrome 测试。
3. 不适合触碰用户真实数据或桌面的工具通过可执行 harness 验证完整路径。
4. 在不破坏现有调用的前提下，增强页面定位、观测、截图、诊断和自动恢复能力。
5. Python 包与 Chrome 扩展统一到 `0.3.0`，以后每个通过测试的完整变更集自动递增 patch。
6. 用可复现证据达到综合评分 95/100，而不是仅凭功能数量自评。

产品原则是权限最大化、自动化优先和最小人工操作。默认 `lab` 模式不发起
elicitation；需要逐次确认的使用者显式切换到 `safe`。

## 非目标

本阶段不新增独立 MCP 工具。以下能力记录到阶段 B，等 0.3.x 稳定后实施：

- 独立 locator/query 工具
- `page_hover`、`page_select`、`page_wheel` 等新输入工具
- 独立下载查询、等待、取消和恢复工具
- HAR 导出、请求拦截、响应 mock
- 性能追踪、Web Vitals、WebSocket/SSE 专用捕获
- 跨域 iframe 深层 DOM 定位

## 当前基线

- 实际注册工具：55
- README 中登记工具：55，名称集合一致
- 离线测试：500 passed，40 live tests deselected
- live 测试目前主要覆盖导航、等待、滚动、页面扫描、截图、标签页、后台输入、
  Cookie、Storage 和 URL 等待
- Python 服务核心文件约 4942 行，扩展 service worker 约 3576 行
- Python 包版本为 `0.2.2`，扩展版本为 `2.1.3`

## 稳定 API 边界

现有 55 个工具是 0.3.0 的稳定表面：

- 不删除工具
- 不删除或重命名现有参数
- 不改变现有 CSS selector 字符串的含义
- 新参数必须有默认值，旧调用产生相同或更完整的结构化结果
- 预期失败继续使用结构化 `status`，不把正常中断升级为 MCP 会话错误

内部代码按领域提取公共逻辑，但只重构与本目标直接相关的部分：

- session 定向与默认 session 恢复
- 总 deadline 传播
- 扩展响应分类
- debugger lease 和临时资源清理
- locator 解析与坐标计算
- 测试覆盖清单与文档检查

## 页面定位增强

现有 `selector` 参数继续接受 CSS 字符串，同时允许结构化 locator 对象。结构化 locator
支持以下字段：

- `css`
- `role` 与可选 `name`
- `text` 与可选 `exact`
- `label`
- 同源 `frame`
- 开放 Shadow DOM 的 `shadow` 路径

`css`、`role`、`text`、`label` 四种主定位方式必须且只能提供一种。`name` 只与
`role` 配合，`exact` 只控制 `name` 或 `text` 的精确匹配。`frame` 接受单个 locator
或从外到内的 locator 数组；`shadow` 接受从外到内的 CSS host 路径。匹配到多个可操作
目标时返回 `ambiguous`，不派发输入。

同一个 locator 必须由公共解析器处理，首先用于 `page_click`、`page_type` 和
`wait_for`。旧 CSS 调用不经过行为迁移。未找到、歧义、不可交互和跨域不可达分别返回
明确状态或错误信息，不静默选择第一个危险目标。

阶段 A 支持同源 iframe 和开放 Shadow DOM。跨域 iframe 仍可使用当前 iframe 元素
坐标与 offset 点击；深层 DOM 解析留到阶段 B。

## 现有工具能力补强

### Console

- 完成 `get_console_messages(filter="user")`
- `user` 只保留页面 MAIN/default execution context 的输出
- `all` 或空值保持现有行为
- 分页、clear 和 stop 的行为必须在过滤前后有明确测试

### Network

不新增工具，在 `network_capture_stop` 上增加可选结果过滤：

- URL pattern
- resource type
- HTTP status 范围
- 是否返回 response body

捕获仍由 start 创建有界 ring，stop 必须释放 debugger lease，即使过滤或序列化失败。

### Screenshot

`capture_page_screenshot` 增加：

- full-page
- clip
- JPEG/WebP quality
- 参数组合校验

`capture_desktop_screenshot` 改为与页面截图一致，返回文本元数据和 MCP ImageContent；
`save_path` 只增加磁盘副本，不抑制图片附件。

### Setup diagnosis

`get_setup_status` 和 CLI doctor 返回：

- Python 包版本
- bridge 运行版本
- 扩展 manifest 版本
- 协议/build capability 标识
- 是否检测到旧 bridge
- 是否检测到扩展磁盘版本与运行版本不一致

能自动重启或恢复的情况自动处理。Chrome 不会从磁盘自动重读 unpacked extension 的情况
如实报告为唯一需要人工 reload 的平台限制。

### Automation profile

- 默认模式继续为 `lab`
- `lab` 默认 `no_elicit=true`
- `safe` 每次物理输入和站点 allow 单独 elicitation
- 两种模式始终保留跨进程锁、用户输入安静窗口、前台确认和 ownership 保护
- `get_automation_profile` 的描述字段必须与实际行为一致

## 55 工具覆盖矩阵

新增机器可读的工具覆盖清单。工具集合以 `mcp.list_tools()` 的实际结果为唯一真源。
每个工具记录：

- schema/defaults 测试
- 成功行为测试
- 参数或失败状态测试
- 测试层：offline、harness、live
- 修改状态时的 cleanup 测试
- README 英文条目
- README 中文条目
- 调用方 skill 条目或分组

门禁要求：

- 实际工具与矩阵必须 55/55 完全相等
- 矩阵不得登记不存在的工具
- 每个工具至少有一条成功行为测试和一条边界/失败测试
- 修改浏览器或文件状态的工具必须验证 cleanup
- 非 destructive 且 Chrome API 可稳定执行的工具必须有 live 测试
- 只能 harness 的工具必须登记原因，不能用“暂未实现”作为原因

## 测试分层

### Contract

逐工具验证注册、description、schema、默认值、版本和文档同步。任何新增参数未同步三处
文档时测试失败。

### Offline

覆盖输入校验、session 定向、deadline、响应分类、并发、ACK 丢失、ownership、文件
原子性、恢复和 cleanup。离线测试不依赖浏览器、bridge 或网络。

### Executable harness

下列能力不操作用户真实对象：

- `set_extension_enabled`
- `uninstall_extension`
- `call_extension`
- `mouse_move`、`mouse_click`、`mouse_drag`、`type_text`、`hotkey`
- 其它会影响用户桌面或不可自动安装测试依赖的路径

harness 必须实际执行扩展 JavaScript 或 Python action 流程，验证 Chrome API 参数、响应
分类、批准策略、锁和失败清理；不能只断言源码包含某个字符串。

### Live Chrome

live 测试使用共享 `scratch_session`、本地 HTTP fixture、临时下载目录、临时书签目录和
临时权限 lease。测试前记录 active tab，结束后恢复并删除所有测试对象。

重点新增 live 覆盖：

- native download
- network capture
- console capture 与 `filter=user`
- save PDF
- site permissions 与 reset
- bookmarks
- debugger targets 与 CDP batch
- upload files
- full-page/clip screenshot
- setup/version diagnosis

## 自动化与清理原则

- 测试和工具不得要求使用者手工创建、关闭或恢复 tab
- 临时 tab 必须带 owner capability，并按 generation 清理
- 临时书签、下载、PDF、上传文件和权限 lease 必须在 `finally` 语义下清理
- live 测试结束必须恢复原 active tab
- debugger capture 必须释放 lease
- 超时后不得盲目重试可能已有副作用的操作
- 只有 Chrome unpacked extension 从磁盘重载这一平台限制允许请求人工操作

## 统一版本与自动升级

版本采用单一真源。Python package metadata、运行时 `__version__` 和 extension manifest
由脚本同步生成或更新。

单一真源为 `src/agent_browser_mcp/_version.py` 中的 `__version__`。`pyproject.toml`
改为通过 setuptools dynamic version 读取该属性；同步脚本读取同一属性并更新 Chrome
extension manifest。bridge 和诊断接口直接报告该运行时版本，不维护第四份常量。

本轮执行 minor bump，统一为 `0.3.0`。之后使用 `python -m scripts.finalize_change`：

1. 检查工作区变更和版本一致性
2. 运行规定的离线测试和静态门禁
3. 只有全部通过才递增 patch
4. 原子写入所有版本位置
5. 再运行版本同步检查

普通保存文件和普通 `pytest` 不修改版本。完整变更集完成时调用 finalizer，避免一次开发
产生大量无意义版本。

CI 比较基线分支：源代码或工具行为变化而版本未增加时失败；版本位置不一致时失败。

## 文档同步

工具签名、默认值或行为变化必须同时更新：

1. `README.md`
2. `README.zh-CN.md`
3. 工具 `description=`
4. `browser-mcp-default/SKILL.md`

已安装的调用方 skill 副本需与仓库内的规范副本一致，最后校验各处 MD5。副本装在哪属于
本机配置，不记录在仓库里。

新增自动检查脚本验证：

- 实际 55 工具与 README 工具名一致
- 覆盖矩阵为 55/55
- 版本完全一致
- 三份调用方 skill 一致
- 新增签名参数在中英文工具表中出现

## CI

新增 GitHub Actions：

- Python 3.10、3.11、3.12、3.13 离线测试
- `compileall`
- package dependency check
- 工具覆盖矩阵检查
- 文档和版本同步检查
- Python coverage 报告和最低门禁，阈值根据首次完整测量设为不低于当前覆盖率，之后只升不降

live Chrome 测试作为独立 workflow，支持本机和配置好 unpacked extension 的自托管
runner。普通 PR 不因缺少真实浏览器 runner 而伪装执行 live 测试。

## 95 分验收

| 维度 | 分值 | 验收证据 |
|---|---:|---|
| 工具覆盖与测试 | 20/20 | 55/55 矩阵、全部规定测试通过 |
| 可靠性与 cleanup | 20/20 | deadline、并发、ownership、lease、临时资源测试 |
| 自动化体感 | 19/20 | lab 默认免询问、自动恢复、仅扩展 reload 保留人工 |
| 页面操作与观测 | 18/20 | locator、Console/Network filter、截图增强 |
| 权限与可控性 | 9/10 | token、profile、租约、owned-tab 和 safe opt-in |
| 文档、版本、CI | 9/10 | 自动版本、同步门禁、CI 和覆盖报告 |
| 合计 | 95/100 | 所有门禁均通过 |

## 完成定义

满足以下条件才发布 0.3.0：

- 工具数仍为 55
- 覆盖矩阵显示 55/55
- 全部离线测试通过
- 全部 executable harness 通过
- 新增 live 测试在已重载当前扩展的真实 Chrome 上通过
- live 测试后用户原 active tab 和浏览器对象已恢复
- Python、bridge、扩展版本均为 `0.3.0`
- README、中文 README、工具 description 和调用方 skill 已同步
- CI、版本门禁和文档门禁通过
- 生成最终按工具测试报告和 95 分验收报告
