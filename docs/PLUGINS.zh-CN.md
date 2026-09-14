# BrowserTap 插件

[English](PLUGINS.md) | 简体中文

Claude Code 和 Codex 插件一起安装 BrowserTap MCP 服务与两份调用方 Skills，
共用[通用 MCP 安装](../README.zh-CN.md#快速开始)的 Python 核心和浏览器扩展。

## 安装

使用支持 `plugin` 命令的新版 Claude Code 或 Codex，并安装
[uv](https://docs.astral.sh/uv/getting-started/installation/)，确保它在 `PATH` 中。
首次启动由 uv 准备 Python 和依赖，需要联网，耗时通常比后续启动长。

Claude Code：

```bash
claude plugin marketplace add LinVireo/browsertap-mcp
claude plugin install browsertap-mcp@browsertap
```

Codex：

```bash
codex plugin marketplace add LinVireo/browsertap-mcp
codex plugin add browsertap-mcp@browsertap
```

使用本地源码包或 checkout 时，把 marketplace 命令中的 `LinVireo/browsertap-mcp`
换成对应的绝对目录；注册期间保留该目录。使用干净的公开源码树，宿主还可能发现目录里的
`.mcp.json` 和其他本地 agent 配置。

安装后新开 agent 会话，让它调用 `get_setup_status` 并显示 `extension_path`。
在 `chrome://extensions` 开启**开发者模式**，选择**加载已解压的扩展程序**，选中该目录。
Edge 和 Opera 分别使用 `edge://extensions` 和 `opera://extensions`。
浏览器扩展首次需要手动加载；agent 侧由插件配置。

随后可让 agent：**“列出我打开的标签页，总结我选定的页面。”**
插件内的 Skills 提供工具选择、标签页归属和连接恢复流程。

如果这个客户端已有手动配置的 BrowserTap MCP 服务，切换到插件时移除该重复配置。
其他客户端可以继续使用原有 MCP 配置，共享同一个桥。

## 命令行与恢复

插件通过隔离的 uv 环境运行自带源码，并包含 `desktop` 依赖，无需另装全局
`browsertap` 命令。管理操作复用插件清单里的启动命令。
例如，把 `<plugin-root>` 替换成宿主实际安装插件的目录：

```bash
uv run --isolated --no-project --no-env-file --python 3.13 --with-editable "<plugin-root>[desktop]" python -m browsertap_mcp.cli doctor
uv run --isolated --no-project --no-env-file --python 3.13 --with-editable "<plugin-root>[desktop]" python -m browsertap_mcp.cli extension-path
```

Windows 同样把路径和 `[desktop]` 一起放在引号内。
Claude 清单里的 `${CLAUDE_PLUGIN_ROOT}` 由 Claude 展开；Codex 用插件根目录作为
`cwd`，参数为 `.[desktop]`。独立终端命令需填实际路径，
可从宿主的插件详情或缓存信息获取。

启动 MCP 服务会自动拉起缺失的桥。`doctor` 要求重启桥时，把上述命令末尾的 `doctor`
换成 `bridge --restart`。按报告的 `action` 处理：旧 MCP 进程需要新会话，
扩展代码改变需要到浏览器手动 **Reload**。重启共享桥会影响其他已连接客户端。
详细诊断见[故障排查](TROUBLESHOOTING.zh-CN.md)。

## 更新

刷新 marketplace 并更新插件，然后新开 agent 会话。

Claude Code：

```bash
claude plugin marketplace update browsertap
claude plugin update browsertap-mcp@browsertap
```

Codex：

```bash
codex plugin marketplace upgrade browsertap
codex plugin remove browsertap-mcp@browsertap
codex plugin add browsertap-mcp@browsertap
```

替换插件缓存前，先关闭使用它的会话。本地 marketplace 先更新源目录；marketplace upgrade
命令刷新的是 Git 来源。新会话调用 `get_setup_status` 后按 `action` 操作。
若插件缓存变化导致 `extension_path` 改变，重新选择新目录加载扩展；路径没变则点 **Reload**。
发布工具会同步插件、Python 包和浏览器扩展版本。

卸载 agent 集成使用宿主的 plugin uninstall/remove 命令。如果其他客户端也不再使用，
再到浏览器移除 BrowserTap Bridge；共享状态清理见[通用卸载说明](../README.zh-CN.md#卸载)。

## 插件内容

| 组件 | 来源 |
| --- | --- |
| Claude Code 清单与 marketplace | `.claude-plugin/` |
| Codex 清单 | `.codex-plugin/plugin.json` |
| Codex marketplace | `.agents/plugins/marketplace.json` |
| 插件发现用 Skills | 根 `skills/`，由 `src/browsertap_mcp/skills/` 生成 |
| MCP 服务与浏览器扩展 | `src/browsertap_mcp/` |

源码发行包包含上述文件；wheel 继续服务通用 MCP 安装。
开发者本机的私人 Skills、hooks 和 agent 设置不进入公开插件包。
