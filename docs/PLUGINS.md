# BrowserTap plugins

English | [简体中文](PLUGINS.zh-CN.md)

The Claude Code and Codex plugins install the BrowserTap MCP server and its two
caller Skills together. Both use the same Python package and browser extension
as the [standard MCP installation](../README.md#getting-started).

## Install

Use a current Claude Code or Codex release with `plugin` commands, and install
[uv](https://docs.astral.sh/uv/getting-started/installation/) on your `PATH`.
uv provisions Python and dependencies on first launch, so that launch needs
network access and can take longer than subsequent starts.

Claude Code:

```bash
claude plugin marketplace add LinVireo/browsertap-mcp
claude plugin install browsertap-mcp@browsertap
```

Codex:

```bash
codex plugin marketplace add LinVireo/browsertap-mcp
codex plugin add browsertap-mcp@browsertap
```

For a local source archive or checkout, replace `LinVireo/browsertap-mcp` with
its absolute directory in the marketplace command. Keep that directory in place
while the local marketplace is registered. Use a clean public source tree:
hosts can also discover local `.mcp.json` and agent configuration files.

Open a new agent session after installation. Ask it to call `get_setup_status`
and show the `extension_path`. In `chrome://extensions`, enable **Developer
mode**, choose **Load unpacked**, and select that directory. Edge and Opera use
`edge://extensions` and `opera://extensions`. Browser extension installation is
a one-time manual step; the plugin provisions the agent side.

Then ask: **"List my open tabs and summarize the page I choose."** The plugin's
Skills guide tool selection, target ownership and connection recovery.

If this client already has a manually configured BrowserTap MCP server, remove
that duplicate entry when switching to the plugin. Other clients may continue
using their existing MCP configuration and share the bridge.

## CLI and recovery

The plugin runs its own bundled source in an isolated uv environment, including
the `desktop` extra. It does not need a global `browsertap` executable. The
plugin manifests contain the exact launch command; reuse it for management.
For example, replace `<plugin-root>` with the installed plugin directory:

```bash
uv run --isolated --no-project --no-env-file --python 3.13 --with-editable "<plugin-root>[desktop]" python -m browsertap_mcp.cli doctor
uv run --isolated --no-project --no-env-file --python 3.13 --with-editable "<plugin-root>[desktop]" python -m browsertap_mcp.cli extension-path
```

Keep the quoted path and `[desktop]` together, including on Windows. The
`${CLAUDE_PLUGIN_ROOT}` placeholder in the Claude manifest is resolved by Claude;
Codex runs with the installed plugin root as `cwd` and uses `.[desktop]`. A
standalone terminal command needs the actual path. Plugin details/cache
information in the host identifies the installation directory.

Starting the MCP server starts a missing bridge. If `doctor` requests a bridge
restart, append `bridge --restart` in place of `doctor` above. Follow the
reported `action`: an old MCP process needs a new session, and changed extension
code needs a manual browser **Reload**. Restarting a shared bridge affects its
other connected clients. See [troubleshooting](TROUBLESHOOTING.md) for details.

## Updates

Refresh the marketplace and update the plugin, then open a new agent session.

Claude Code:

```bash
claude plugin marketplace update browsertap
claude plugin update browsertap-mcp@browsertap
```

Codex:

```bash
codex plugin marketplace upgrade browsertap
codex plugin remove browsertap-mcp@browsertap
codex plugin add browsertap-mcp@browsertap
```

Close sessions using this plugin before replacing its cache. For a local
marketplace, update the source directory first; the marketplace upgrade command
refreshes Git sources. Ask the new session for `get_setup_status` and follow its
`action`. If `extension_path` changed with the plugin cache, load the extension
from the new directory; if the path stayed the same, use **Reload**. Plugin,
Python package and extension versions are kept in sync by the release tooling.

Use the host's plugin uninstall/remove command to remove the agent integration.
Remove BrowserTap Bridge separately in the browser if no other client uses it.
The [standard uninstall guide](../README.md#uninstall) covers shared state.

## Contents

| Component | Source |
| --- | --- |
| Claude Code manifest and marketplace | `.claude-plugin/` |
| Codex manifest | `.codex-plugin/plugin.json` |
| Codex marketplace | `.agents/plugins/marketplace.json` |
| Plugin discovery Skills | `skills/`, generated from `src/browsertap_mcp/skills/` |
| MCP server and extension | `src/browsertap_mcp/` |

The source distribution contains these files. The wheel continues to serve
standard MCP installations. Contributor-local Skills, hooks and agent settings
are outside the public plugin bundle.
