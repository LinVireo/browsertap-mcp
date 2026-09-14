# Tool contracts

Read before changing a public tool signature, default, behavior, caller guidance,
or the way packaged Skills are installed and checked.

Start with [AGENTS.md](../../AGENTS.md) for the task entry point. Numbered
sections retain the original guide's references; other section numbers refer
to that root guide. Commands and release gates remain in
[CONTRIBUTING.md](../../.github/CONTRIBUTING.md).

## 2. Changing a tool signature or a default touches four places

Miss one and some agent keeps calling the tool from outdated instructions:

1. the `## Tools` table in **both** `README.md` and `README.zh-CN.md`
2. the tool's own `description=` in `server.py` -- this is the text a calling
   agent actually receives
3. `src/browsertap_mcp/skills/browsertap-default/SKILL.md` -- the **caller's**
   rules of engagement (which tool to call first, when `session_id` is
   mandatory). This one decides whether other agents misuse the server.
4. `src/browsertap_mcp/skills/browsertap-bridge-recovery/SKILL.md` -- what to do when
   the transport itself is down.

The two skills **cross-reference each other** (`[[...]]`); updating only one
points the reader at advice that no longer holds. Phrases that must survive an
edit are listed in `REQUIRED_SKILL_TEXT` in `scripts/check_tool_docs.py`. Never
hardcode an extension version in a skill: the extension and the package share
one version now, so the reader must compare
`get_setup_status.package_version` at runtime.

The gate for all of the above:

```bash
python -m scripts.check_tool_docs
```

The skills ship inside the wheel as package data.
`browsertap skill-path` prints the directory that holds them as
`<name>/SKILL.md`. Point your skill manager **at that directory** instead of
copying the files: a copy reads as correct for as long as the contents happen to
agree and then silently stops receiving updates. If you keep a copy anyway,
`python -m scripts.check_tool_docs --check-installed-skills --skill-mirror DIR`
compares the hashes and tells you which skill drifted. Asking for that check
without naming a directory fails on purpose rather than passing vacuously.
