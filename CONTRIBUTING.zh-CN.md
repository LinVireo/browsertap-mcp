# 贡献指南

[English](CONTRIBUTING.md) | 简体中文

提交的改动应保持 ABM 的核心行为：操作用户正在使用的真实浏览器会话，优先使用后台
页面/CDP 能力，只有在明确且确实必要时才使用前台物理输入。

## 开发环境

```text
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,desktop]"  # Windows PowerShell
python -m pip install -e ".[dev,desktop]"                       # 其他已激活的虚拟环境
agent-browser-mcp extension-path
```

将命令输出的目录作为未打包扩展加载。editable 安装会立即读取 Python server 改动；
bridge 改动需要重启 bridge；扩展源码改动需要在浏览器扩展管理页手动重新加载。

## 测试

常规测试为离线测试，不会操作浏览器：

```text
python -m ruff check src tests scripts
python -m pytest tests -q
python -m pytest tests -q --cov=agent_browser_mcp --cov-fail-under=85
python -m scripts.tool_coverage_report --format markdown
python -m scripts.check_tool_docs --format markdown
python -m scripts.versioning check
python -m build --wheel --sdist --outdir artifacts/dist
python -m scripts.check_distribution artifacts/dist
```

这就是 `scripts/finalize_change.py` 与 `.github/workflows/test.yml` 使用的顺序；最后两条
必须成对执行：`check_distribution` 检查的正是上一条 build 写出的归档，单独运行只会报
`no wheel found`，不是通过。

门禁规则集是 `ruff check`。`ruff format` 不是门禁，且现有源码大多不符合它的格式，
对只做局部修改的文件跑一遍会让无关的重排淹没本次改动。请按周围代码的既有风格书写。

live 测试必须显式运行：

```text
python -m pytest tests -q -m live
```

live 测试会操作已连接的真实浏览器，并可能暂时影响前台。只能在准备好的机器上运行；
运行前记录用户当前激活的标签页，复用共享 scratch fixture，结束后核对清理与现场恢复。
不得为 live 测试增加 headless 或 Playwright 回退路径，因为它们验证的是另一套产品契约。

公开的 `test.yml` 只在 GitHub 托管 runner 上运行离线门禁。`live.yml` 只能手动触发，
目标是预先配置的 Windows 自托管 runner。若 runner 的 `python` 不是指定解释器，应设置
仓库变量 `ABM_LIVE_PYTHON`。不要把日常交互使用的桌面配置为定时运行 live 的 runner。
live job 仅允许规范仓库和 `abm-live` GitHub environment；注册 runner 前，应为该
environment 配置 required reviewer。workflow 文件本身无法创建或强制执行仓库的
environment 保护规则。

本地生成发布候选时，应在准备发布的同一棵源码树上运行完整离线管线：

```text
python -m scripts.finalize_change --bump none --skip-live
python -m scripts.evidence_manifest --check
```

finalizer 会先把上一轮证据移动到带时间戳的 `artifacts/archive/`，再生成一套规范证据：
offline JUnit、覆盖率、工具证据，以及各一份 wheel/source archive。
`artifacts/evidence-manifest.json` 将这些文件与 Git HEAD、dirty 状态和公开源码树内容哈希
绑定。此后任何源码编辑或提交都会使证据过期；公开发布前必须在最终 commit 上重新运行
完整管线。

这条绑定里有两个性质决定报告是否可信：

- manifest 记录 `schema_version`。构成指纹的字段发生变化时，校验器按**版本**直接拒绝旧
  manifest，而不是报告内容不一致 —— 输入不同的指纹之间没有可比性。输出里的
  `re-seal with the current tooling` 就是这个含义：重新生成证据，不要去找那个"改动了"
  某个文件的编辑。
- 在 dirty 工作树上封存的证据会记录 `git_dirty: true`，验收报告把它直接算作证据问题。
  存在未提交或未跟踪文件时，仅凭 `git_head` 无法确定产出这批证据的代码，因此 dirty
  封存永远不可能达到 `release_ready`。先提交发布面，再封存。

这些产物只应由 finalizer 写入。手工把 `--junitxml`/`--cov` 指向 `artifacts/` 跑单个门禁，
会覆盖已封存证据中的一部分而留下其余部分，`--check` 随后会把它报成不一致。

## 工具契约变更

工具名称、参数、默认值或行为发生变化时，必须在同一个改动中同步：

1. `README.md` 与 `README.zh-CN.md` 中作为权威列表的 55 个工具说明；
2. 工具自身的 MCP `description=` 文本；
3. 仓库内的调用方契约 `docs/browser-mcp-default.SKILL.md`。

维护者还应同步本机已安装的调用方 skill 副本。默认文档门禁校验四件事：仓库内的规范
skill 副本、工具注册、文档里的参数与默认值，以及版本一致性；它不校验本机已安装的副本。
维护者可显式使用 `python -m scripts.check_tool_docs --check-installed-skills` 核对
Agent、Codex 与 Claude 的已安装副本。

调用方 skill 是源码仓库的维护契约，不是 Python 包运行时数据。
`docs/browser-mcp-default.SKILL.md` 保留在 Git 中用于审阅和同步，但明确排除在 wheel 与
source distribution 之外。不得写入本机专属路径或只对某台机器成立的断言。

## 版本与发布卫生

- Python 包、bridge 协议、扩展 manifest、两份 README 与 CHANGELOG 最新版本必须一致。
  提交改动前运行 `python -m scripts.versioning check`。用户可见改动先写入
  `[Unreleased]`；`python -m scripts.versioning bump|sync` 会在发布时生成新版本段并
  更新比较链接。
- 普通 pull request 不需要提升发布版本。CI 的版本增量门禁仅适用于 `release/*` 分支的
  push，使发布协调者统一管理共享版本与 CHANGELOG 文件，避免所有贡献者在这些文件上
  产生冲突。
- `python -m scripts.finalize_change` 会先同步目标版本，再针对该版本运行门禁。
  finalization 后不得修改版本；确需修改时，必须在准确的最终源码树上重新运行测试、
  覆盖率、文档、构建和发行检查。
- 不提交缓存、coverage/JUnit 生成物、日志、本地截图或构建输出。这些可再生或本机文件应
  由 `.gitignore` 排除。
- 旧版 `src/agent_browser_mcp/chrome_extension/config.js`/TID 页面命令通道已删除。
  该文件不得进入 Git 或 Python 发行包，发行门禁会拒绝它。
- 不得包含 bridge token、Cookie、`.env` 文件、浏览器 profile 或复制的用户内容。
  公开仓库发布前，对工作树和完整 Git 历史运行 secret scan。
- wheel 与 source distribution 应作为 GitHub Release 资产上传。不要把它们、本地验收
  报告或 live 浏览器证据提交到 Git。

## Pull Request 检查表

- diff 只覆盖声明的行为，并保留无关的本地改动。
- 离线测试以及文档/版本检查通过。
- 新行为具有成功、边界与清理路径测试。
- 用户标签页不会被登记到 Agent-owned 清理集合。
- 后台操作不会激活标签页或移动光标。
- 公开文档已同步更新英文与中文版本。
- 不包含生成物或 secret。
