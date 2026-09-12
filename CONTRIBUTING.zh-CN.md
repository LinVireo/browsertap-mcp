# 贡献指南

[English](CONTRIBUTING.md) | 简体中文

本指南写给修改或发布 BTAP 的开发者与编码 agent。安装使用从 [README.zh-CN.md](README.zh-CN.md)
开始；调用浏览器工具的 agent 使用随包 skills，修改本仓库的 agent 还需阅读
[AGENTS.md](AGENTS.md) 中的实现约束。

提交的改动应保持 BTAP 的核心行为：操作用户正在使用的真实浏览器会话，优先使用后台
页面/CDP 能力，只有在明确且确实必要时才使用前台物理输入。

参与本项目须遵守[行为准则](CODE_OF_CONDUCT.md)。其中有一条在本项目比在多数项目更
要紧：本工具驱动的是真实浏览器配置文件，所以在 issue 或 pull request 里贴复现步骤
之前，请先把 cookie、令牌、浏览历史和页面截图脱敏。

## 开发环境

在仓库根目录创建并激活虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Linux/macOS 使用 `python -m venv .venv`，然后运行 `. .venv/bin/activate`。
在该环境中安装开发依赖：

```text
python -m pip install -e ".[dev,desktop]"
browsertap extension-path
```

如果无法激活环境，下文命令使用该虚拟环境中可执行文件的完整路径。
将命令输出的目录作为未打包扩展加载。editable 安装指向当前源码目录；
Python server 改动需要重启 MCP 会话，bridge 改动需要重启 bridge；
扩展源码改动需要在浏览器扩展管理页手动重新加载。

### 提交钩子

可选的 [pre-commit](https://pre-commit.com/) 配置会执行仓库现有的 lint、工具文档、
版本、暂存区空白和密钥检查。先将 Gitleaks 安装到 `PATH`、运行 `npm ci`，
并使用已激活的开发环境：

```text
python -m pip install "pre-commit>=3.2,<5"
pre-commit install
pre-commit run --all-files
```

本地钩子直接使用该环境的工具，不会另装一套 lint 工具链；缺少工具会失败。
lint 报告写入 `out/pre-commit/lint.json`，后续检查要求 JavaScript 与类型检查
确实执行。即使传 `--all-files`，Gitleaks 检查的仍是暂存差异。
钩子不运行测试、构建、浏览器操作或发布封存；提交前仍须执行下文中与改动相关的检查。

## 测试

默认测试是离线的，不操作用户的浏览器。在仓库根目录使用开发环境的 Python 运行：

```text
npm ci
python -m scripts.lint_report
python -m pytest tests -q
python -m pytest tests -q --cov=browsertap_mcp --cov-fail-under=95
python -m scripts.tool_coverage_report --format markdown
python -m scripts.check_tool_docs --format markdown
python -m scripts.versioning check
python -m build --wheel --sdist --outdir artifacts/dist
python -m scripts.check_distribution artifacts/dist
python -m scripts.check_install artifacts/dist --no-deps
```

使用空的构建输出目录。先 build，再检查或安装归档；`check_distribution` 要求目录里
恰好一份 wheel 和一份 sdist，并核对两者的包文件集合，以发现旧 `build/` 混入退役文件
或某个归档漏包的问题。

最后一条命令是无法下载依赖时的**布局检查**。能访问依赖索引时，还应运行
`python -m scripts.check_install artifacts/dist`：它在仓库之外的新环境安装依赖，
实际执行 CLI 并检查随包路径。看报告的 `mode` 和 `proves_cli`；
`--no-deps` 通过不等于 CLI 已验证可运行。

### 各门禁证明什么

| 门禁 | 通过条件 |
| --- | --- |
| 测试 | 成功、失败和清理路径符合断言；行为变更有对应回归用例。 |
| 覆盖率 | 行与分支合并后的总体至少 95%，不代表每个指标或每个文件都达到 95%；验收另查 `PER_FILE_COVERAGE_FLOOR`，缺少逐文件数据也失败。 |
| Ruff | 检查 `src/`、`tests/`、`scripts/` 的 Python；每个目标都必须实际贡献文件。 |
| ESLint | 检查扩展及注入页面的 JavaScript，并分别核对目标实际启用的规则。 |
| mypy | 检查所有随包 Python 源码，包括没有签名注解的函数体；缺失或不完整检查会失败。 |
| 文档 | 已注册工具、参数、默认值、双语 README、调用方 skills 和版本一致。 |
| 发行与安装 | 归档包含必需文件、排除本机数据；完整安装检查还验证安装后的可用性。 |

`scripts/lint_report.py` 是 Ruff、ESLint 和 mypy 的共用入口。
`artifacts/lint.json` 将 Python lint、`javascript`、`types` 分别记录，
包含文件数和是否实际执行。ESLint 需要 `npm ci`，mypy 来自 `.[dev]`。
缺少 JavaScript 工具链时报告 `unavailable`，Python 检查仍可能通过，但发布验收会拒绝；
缺少 mypy、存在类型错误或检查不完整会令命令直接失败。

单独检查类型可用 `python -m mypy src`。代码格式遵循周围风格；
`ruff format` 不是门禁，对局部修补进行全文件格式化会淹没实际修改。

### Live 测试

```text
python -m pytest tests -q -m live
```

live 需要准备好的真实浏览器，可能影响前台。复用共享 scratch fixture，不要每个测试
都开新标签页；headless 或 Playwright 回退验证的不是同一套产品。

session fixture 通过 `get_setup_status()` 和 `tests/live_preflight.py`
核对 MCP 进程、bridge、扩展是否匹配受测源码。过期组件会令测试失败，并指出所需的
重启或手动 Reload。用户浏览器活动只作为上下文记录，不禁止用户开、关或导航自己的标签页。

收尾检查 `server._TAB_OWNERSHIP.outstanding()`，只把套件自己创建却未关闭的标签页
判作泄漏。失败会同时提示两种可能：缺少带 owner 的清理，或关闭被
`lifecycle generation changed` 拒绝。`own_tabs.enforced` 区分实际检查与未持有任何标签页。
构建身份、浏览器活动和所有权计数写入 `artifacts/live-preflight.json`，与 live JUnit 一起绑定。

`test.yml` 在 GitHub 托管 runner 上跑离线检查。`live.yml` 只能手动触发，
限定规范仓库、`btap-live` environment 和准备好的 Windows 自托管 runner。
使用前配置 required reviewer；workflow 不能替仓库创建 environment 保护。
解释器不在默认 `python` 路径时设置 `BTAP_LIVE_PYTHON`。

### 发布证据

准备发布候选时再运行 finalizer，日常迭代不对移动中的工作树封存：

```text
python -m scripts.finalize_change --bump none --skip-live
python -m scripts.evidence_manifest --check
python -m scripts.check_release_tag --allow-missing-tag
```

使用 `--bump none` 的前提是版本已经合适，见[版本与发布卫生](#版本与发布卫生)。
`--skip-live` 不代表 live 已验证，也不代表可以发布。

finalizer 把旧证据归档到 `artifacts/archive/`，然后生成规范报告和发行包。
`artifacts/evidence-manifest.json` 绑定 Git HEAD、dirty 状态和公开文件哈希。
先提交拟发布内容再封存；`git_dirty: true` 不能达到 `release_ready`。
后续编辑或提交会使证据过期；manifest schema 不匹配时也须用当前工具重新生成。

保留已有封存件。临时验证的报告写入新的 `out/` 子目录，避免覆盖 `artifacts/`
中的单个文件并与旧证据混用。

## 工具契约变更

工具名称、参数、默认值或行为发生变化时，必须在同一个改动中同步：

1. `README.md` 与 `README.zh-CN.md` 中作为权威列表的 51 个工具说明；
2. 工具自身的 MCP `description=` 文本；
3. 调用方契约 `src/browsertap_mcp/skills/browsertap-default/SKILL.md`
   （先调哪个工具、什么时候必须带 `session_id`）；
4. `src/browsertap_mcp/skills/browsertap-bridge-recovery/SKILL.md`
   （桥本身连不上时调用方该怎么恢复）。

两份 skill 互相引用，因此各自按「一组副本」独立做哈希校验 —— 只更新其中一份，读者会被
指向已经不成立的说明。两份都不得写入本机专属路径或只对某台机器成立的断言；写了绝对路径
会被 `tests/test_documentation_contract.py` 拦下。

它们以 package data 形式随包发布，因此 `pip install browsertap-mcp` 就带着它们，
`browsertap skill-path` 会打印存放目录（形如 `<name>/SKILL.md`）。`MANIFEST.in`
的规则与 `pyproject.toml` 的 `package-data` 通配**两者都必需**：前者管 source archive，
后者管 wheel；只写一处会得到「sdist 里有、wheel 里没有」，而 `pip install` 用的正是 wheel。
`scripts/check_distribution.py` 要求两个归档里都有这两份文件，并拒绝归档中其他位置出现的
`SKILL.md`。

skill 管理器应**指向随包发布的那个目录**，不要复制文件。复制出来的副本在内容恰好一致期间
看不出问题，之后就静默收不到更新 —— 哈希校验就是为了抓这种漂移。如果确实保留了副本，在加
`--check-installed-skills` 的同时给出副本所在目录：

```bash
python -m scripts.check_tool_docs --check-installed-skills \
    --skill-mirror /path/to/installed/skills
# 或：BROWSERTAP_SKILL_MIRRORS="dir1:dir2" python -m scripts.check_tool_docs --check-installed-skills
```

每个目录下应有 `<skill-name>/SKILL.md`。agent 客户端把 skill 装在哪属于本机配置，
本仓库不记录这些路径；只加开关却不给目录会直接失败，不会静默通过。不加任何开关的默认门禁
校验四件事：随包发布的 skill、工具注册、文档里的参数与默认值，以及版本一致性 —— 也就是
没有已安装副本的贡献者能验证的全部内容。

## 版本与发布卫生

- Python 包、bridge 协议、扩展 manifest、两份 README、MCP Registry manifest
  （`server.json`）与 CHANGELOG 最新版本必须一致。提交改动前运行
  `python -m scripts.versioning check`。用户可见改动先写入 `[Unreleased]`；
  `python -m scripts.versioning bump|sync` 会在发布时生成新版本段并更新比较链接。
- `server.json` 与 `src/`、`scripts/` 同属生产文件，改它必须提升版本。它把版本写了两遍：
  顶层那个是注册表展示的标签，`packages[]` 里那个才是客户端真正安装的版本，两处都会被
  改写并互相比对——只动标签的一次提升会让列表宣称一个版本、交付另一个版本。它的 `name`
  必须与 README.md 里的 `mcp-name` 标记一致，那一对才是注册表认可的命名空间归属证明。
- 普通 pull request 不需要提升发布版本。CI 的版本增量门禁仅适用于 `release/*` 分支的
  push，使发布协调者统一管理共享版本与 CHANGELOG 文件，避免所有贡献者在这些文件上
  产生冲突。
- `python -m scripts.finalize_change` 会在本地跑同一条增量检查，基线取最近一个发布 tag，
  比较对象是**工作树**。CI 那条是拿一次 push 与它前一个提交比，仓库从未 push 过时它永远
  不触发；而提交范围比较在改动尚未提交时看到的是"什么都没改" —— 于是一整轮真实行为变更
  可能沿用起始版本号被封存。本地这条会直接拒绝，并打印它用的基线；仅在仓库还没有任何 tag
  时跳过。
- `python -m scripts.finalize_change` 会先同步目标版本，再针对该版本运行门禁。
  finalization 后不得修改版本；确需修改时，必须在准确的最终源码树上重新运行测试、
  覆盖率、文档、构建和发行检查。
- 不提交缓存、coverage/JUnit 生成物、日志、本地截图或构建输出。这些可再生或本机文件应
  由 `.gitignore` 排除。
- 旧版 `src/browsertap_mcp/chrome_extension/config.js`/TID 页面命令通道已删除。
  该文件不得进入 Git 或 Python 发行包，发行门禁会拒绝它。
- 不得包含 bridge token、Cookie、`.env` 文件、浏览器 profile 或复制的用户内容。
  `.github/workflows/supply-chain.yml` 每次 push 都会扫描工作树与完整 Git 历史，
  用的 gitleaks 同时锁定版本**和** sha256。公开仓库发布前，本地也跑同样两条：

  ```bash
  gitleaks git . --no-banner --redact
  gitleaks dir . --no-banner --redact
  ```

  事后真正起作用的是历史那一半：提交过又删掉的 secret 依然是公开的，只有改写历史能
  移除它，再补一个提交不行。`--redact` 保证扫描器不会把它发现的 secret 打进任何人都
  能读到的构建输出。
- 每个第三方 action 都钉到 commit SHA，并在行尾注释里写明对应版本，
  `tests/test_supply_chain.py` 会拒绝浮动 tag（仅有一个有据可查的例外）。SHA 自己没有
  更新通道，所以 `.github/dependabot.yml` 就是那条通道：只管 actions 生态、每月一次、
  合并成一个 pull request。Python 侧**故意不接**——上面那条审计已经覆盖，而 `dev` extra
  的上界正是用来阻止门禁工具链自己漂移的，机器人把它们抬上去等于取消它们的作用。
- 同一个 workflow 还会解析 `pip install` 实际拉进来的依赖闭包、对着漏洞库审计它，并把
  CycloneDX SBOM 作为构建产物发布。该审计在这里是提示性的，在 `release.yml` 里是阻断性
  的：一夜之间新增的公告不该让所有分支变红，但它确实是"这个版本先别发"的正当理由。
- wheel 与 source distribution 应作为 GitHub Release 资产上传。不要把它们、本地验收
  报告或 live 浏览器证据提交到 Git。

## 发布到 PyPI

PyPI 包名是 [`browsertap-mcp`](https://pypi.org/project/browsertap-mcp/)。
分别验证核心安装与可选的 `desktop` extra；普通页面和浏览器工作流必须在没有桌面依赖时可用。

`.github/workflows/release.yml` 负责构建、门禁与上传。它**不会**被 push 触发，只能手动
运行或由已发布的 GitHub Release 触发。原因是上传不可撤销：PyPI 上的文件名永不可复用，
一次错误的上传会永久占掉那个版本号，只能改用下一个 patch 版本。

上传之前必须先具备三样东西，且都无法从本仓库内部创建：

1. 一个 PyPI 账号，且项目名 `browsertap-mcp` 已归属自己。这一条已经落定：0.4.12 那次
   上传创建了这个项目，被他人占用的名字本来也接管不了。
2. PyPI 上为本仓库配置的 **Trusted Publisher**：仓库 `LinVireo/browsertap-mcp`、
   workflow `release.yml`、environment `pypi`。Trusted Publishing 的含义是 workflow 在
   请求时用短期 GitHub OIDC token 换取上传凭据，仓库里不存任何 API token —— 没有可泄露
   的东西，也不需要轮换。TestPyPI 上按同样方式再配一份，environment 用 `testpypi`。
3. GitHub 上名为 `pypi` 与 `testpypi` 的 environment。给 `pypi` 加上 required reviewer：
   environment 是人工确认这次不可逆上传的最后一道关口。

然后按顺序执行：

```bash
# 1. 先在本地证明这棵树和构建出的归档可发布。
python -m scripts.finalize_change --bump none
python -m scripts.evidence_manifest --check

# 2. 先在 TestPyPI 演练（Actions -> BTAP publish to PyPI -> index: testpypi），再装进一个
#    一次性虚拟环境验证。依赖仍从正式索引取，只有本包来自演练索引。
python -m pip install --index-url https://test.pypi.org/simple/ \
    --extra-index-url https://pypi.org/simple/ "browsertap-mcp[desktop]"

# 3. 发布该 tag 对应的 GitHub Release，完成正式上传。
```

workflow 会针对**即将上传的那批归档**重跑离线测试、文档检查、
`scripts.check_distribution`（必需文件、无本机数据，以及索引渲染与分类所需的元数据）
与 `twine check --strict`。手工构建过归档的话，本地也跑一次
`python -m twine check --strict dist/*`：只有它会按索引的方式渲染长描述，而且是最后一次
还免费的检查。

之后它会把刚构建出的 wheel 装进一个全新虚拟环境、在那里运行命令入口
（`scripts.check_install`），审计这个 wheel 会拉到用户机器上的依赖闭包，并把该闭包的
CycloneDX SBOM 作为**单独**产物写出 —— 单独是因为 publish 作业会把 `dist/` 下的所有东西
上传到索引。

tag 指向的提交若不是封存验收证据的那个提交，发布出去的就是没人验证过的东西。
`python -m scripts.check_release_tag` 用机械方式回答这件事：`v<源码版本>` 不存在、指向
别的提交（会同时报出两个 sha、相差几个提交、哪些生产文件不同），或者仍有未提交的生产
文件（任何 tag 都无法描述还没进提交的文件）时，它都会失败。`release.yml` 在安装和构建
任何东西之前先跑它。打完 tag、发布 Release 之前请自己也跑一次，并确认封存报告里的
`verified_at` 就是那个提交。

## 在 MCP Registry 上架

`server.json` 就是那份列表条目。注册表只存元数据、不存构件，所以它只能在对应版本
**已经上传到 PyPI 之后**提交——`packages[]` 里的版本会拿去索引上解析。

PyPI 包的归属证明是一段 `mcp-name: <服务名>` 字符串，位置在"会成为 PyPI 包描述的那份
README"里，本项目即 `README.md`（`pyproject.toml` 中 `readme = "README.md"`）。它在第 1 行、
写在 HTML 注释里，`server.json` 的 `name` 必须与它完全一致；
`tests/test_documentation_contract.py` 会检查这一对。标记后面必须跟一个边界字符，所以让它
单独占一行——在结尾粘一个句号就匹配不上了。用 GitHub 认证时，名字还必须以
`io.github.<owner>/` 开头。

提交由注册表自己的 `mcp-publisher` CLI 完成：`login`（GitHub 是它支持的多种方式之一）、
`validate`（只校验 manifest 是否符合已发布 schema，不提交）、然后在仓库根目录 `publish`。
先跑 `validate`：JSON Schema 本身只在那里被强制，本仓库只门禁它能在本地证明的字段——
两处版本、identifier、registryType 与 repository URL——并不把 schema 拷进来。

注册表目前是 preview 状态，官方声明在正式发布前可能重置数据，所以把一次成功上架当成
可重做的事、不是永久的。每个新版本都要重新提交一次：提版本、发 PyPI、再 publish 一次。

## Pull Request 检查表

- diff 只覆盖声明的行为，并保留无关的本地改动。
- 离线测试以及文档/版本检查通过。
- 新行为具有成功、边界与清理路径测试。
- 用户标签页不会被登记到 Agent-owned 清理集合。
- 后台操作不会激活标签页或移动光标。
- 公开文档已同步更新英文与中文版本。
- 不包含生成物或 secret。
