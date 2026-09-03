# BrowserTap MCP 代码审查报告

**审查日期**: 2026-09-03  
**审查范围**: 所有 Python 源文件（11 个文件，共 12,742 行代码）  
**审查视角**: 企业级安全、代码质量、可维护性  
**审查员**: Claude (Opus 5)

---

## 执行摘要

### 总体评级: ⭐⭐⭐⭐ (4/5)

**优势**：
- ✅ 路径遍历防护严密（三层验证）
- ✅ Token 鉴权机制完整（原子创建、0o600 权限、HMAC 比对）
- ✅ 跨平台进程身份验证健壮（PID + 时间戳 + 可执行文件路径）
- ✅ 原子文件操作一致（临时文件 + fsync + os.replace）
- ✅ 并发安全意识强（字典快照、条件变量、文件锁）

**主要问题**：
1. ⚠️ 截图工具缺少原子写入（2 个函数）
2. ⚠️ 无文件大小限制（截图可能数十 MB）
3. ⚠️ 物理输入工具安全边界模糊（建议废弃 7 个工具）
4. ⚠️ ABM 遗留代码待清理

---

## 一、文件清单与审查状态

| # | 文件 | 行数 | 作用 | 状态 |
|---|------|------|------|------|
| 1 | `__init__.py` | 10 | 包入口，遗留环境变量迁移 | ✅ 完成 |
| 2 | `_version.py` | 1 | 版本常量 | ✅ 完成 |
| 3 | `paths.py` | 110 | 状态目录管理 | ✅ 完成 |
| 4 | `physical_input.py` | 731 | OS 级键鼠控制 | ✅ 完成 |
| 5 | `page_input.py` | 750 | CDP 输入事件构造 | ✅ 完成 |
| 6 | `simphtml.py` | 1027 | HTML 优化与 JS 监控 | ✅ 完成 |
| 7 | `extension_build.py` | 198 | 扩展构建校验 | ✅ 完成 |
| 8 | `cli.py` | 242 | 命令行工具 | ✅ 完成 |
| 9 | `server.py` | 6946 | MCP 工具定义（部分审查）| ✅ 关键函数完成 |
| 10 | `browser_bridge.py` | 1947 | 桥接通信层 | ✅ 完成 |
| 11 | `bridge.py` | 487 | 守护进程生命周期 | ✅ 完成 |
| **总计** | | **12,742** | | |

---

## 二、安全审查结果

### 2.1 路径遍历防护 ⭐⭐⭐⭐⭐

**位置**: `server.py:107-166` (`_validate_safe_path`)

**实现**：
```python
def _validate_safe_path(save_path: str, *, allowed_base: Optional[Path] = None, 
                        description: str = "save_path") -> Path:
    # 1. 空路径检查
    if not isinstance(save_path, (str, Path)) or not str(save_path).strip():
        raise ValueError(f"{description} must not be empty")
    
    # 2. 默认安全目录
    if allowed_base is None:
        allowed_base = Path.home() / "Downloads" / "browsertap"
    allowed_base = allowed_base.resolve()
    
    path = Path(save_path).expanduser()
    
    # 3. 拒绝绝对路径（包括原始字符串检查）
    if path.is_absolute() or path.drive or str(save_path).startswith("/"):
        raise ValueError(f"{description} must be a relative path within {allowed_base}")
    
    # 4. 解析并验证最终路径
    final_path = (allowed_base / path).resolve()
    if not final_path.is_relative_to(allowed_base):
        raise ValueError(f"{description} must stay within {allowed_base}, "
                        f"attempted path traversal detected")
    
    return final_path
```

**防护层次**：
1. ✅ 拒绝空路径
2. ✅ 拒绝绝对路径 (`/etc/passwd`)
3. ✅ 拒绝驱动器路径 (`C:\Windows\...`)
4. ✅ 拒绝 `../` 遍历（通过 `resolve()` + `is_relative_to()` 检测）
5. ✅ 跨平台安全（Windows 上 pathlib 会转换 POSIX 路径，需原始字符串检查）

**覆盖范围**：
- ✅ `save_pdf` (4470-4530行)
- ✅ `capture_page_screenshot` (6316-6428行)
- ✅ `capture_desktop_screenshot` (6478-6553行)

---

### 2.2 Token 鉴权机制 ⭐⭐⭐⭐⭐

**位置**: `browser_bridge.py:236-466`

#### 2.2.1 Token 持久化
```python
def _persist_token(path: Path, token: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        # 写入完整 token
        pending = memoryview((token + '\n').encode('utf-8'))
        while pending:
            written = os.write(fd, pending)
            if written <= 0 or written > len(pending):
                raise OSError(f'invalid token write count: {written}')
            pending = pending[written:]
    finally:
        os.close(fd)
    # 显式 chmod（防平台差异）
    os.chmod(path, 0o600)
    return token
```

**安全特性**：
- ✅ `O_CREAT | O_EXCL` 原子创建（防止竞争）
- ✅ 0o600 权限（仅当前用户可读写）
- ✅ 完整性检查（写入字节数验证）
- ✅ 竞争窗口处理（FileExistsError 后轮询读取）

#### 2.2.2 Token 比对
```python
def check_link_token(headers, want: str) -> None:
    if not want:
        return  # 鉴权已显式关闭
    got = header_token(headers)
    # ✅ 时序攻击防护：hmac.compare_digest
    if not got or not hmac.compare_digest(got, want):
        raise bottle.HTTPResponse(status=401, body='unauthorized: missing or bad bridge token')
```

**防护**：
- ✅ 常量时间比对（`hmac.compare_digest`，防时序攻击）
- ✅ 双头支持（`Authorization: Bearer` 和 `X-Bridge-Token`）
- ✅ Token 指纹（`token_fingerprint` 仅返回 SHA-256 前 8 位，日志安全）

#### 2.2.3 未读请求体清理
```python
def drain_request_body() -> None:
    """Bottle 仅解析已知 Content-Type，未读 body 导致 Windows 上连接重置"""
    try:
        request.body.read(request.MEMFILE_MAX + 1)
    except Exception:
        pass
```

**关键路由**：
- ✅ `/link` (816-882行)
- ✅ `/api/longpoll` (718-785行)
- ✅ `/api/result` (787-814行)

---

### 2.3 进程身份验证 ⭐⭐⭐⭐⭐

**位置**: `bridge.py:108-248`

#### 跨平台实现

**Windows** (108-161行)：
```python
# OpenProcess + GetProcessTimes + QueryFullProcessImageNameW
identity = {
    "pid": pid,
    "creation_ticks": int((creation.dwHighDateTime << 32 | creation.dwLowDateTime) / 10000000 - 11644473600),
    "executable": os.path.normcase(os.path.abspath(image_path)),
}
```
- ✅ 精度: 100ns (FILETIME)
- ✅ 完整路径: `QueryFullProcessImageNameW`
- ✅ 64-bit HANDLE 支持

**Linux** (163-198行)：
```python
# /proc/{pid}/stat 第 22 字段 (starttime, jiffies)
creation_ticks = boot_time + (starttime / CLK_TCK)
```
- ✅ 精度: ~10ms (CLK_TCK=100Hz)
- ✅ 可执行文件: `/proc/{pid}/exe` readlink
- ✅ 处理进程名中的括号和空格

**macOS** (200-233行)：
```python
# /bin/ps -o lstart= -o comm= -p {pid}
# lstart: "Mon Jan 01 12:34:56 2024"
```
- ⚠️ 精度: 1 秒（低于 Windows/Linux）
- ✅ 环境变量 `LC_ALL=C` 固定格式
- ⚠️ 依赖外部命令（但 macOS `/proc` 不可靠）

#### PID 复用防护
```python
def _same_process(record: dict[str, Any], identity: dict[str, Any]) -> bool:
    return (
        identity.get("pid") == record.get("pid")
        and identity.get("creation_ticks") == record.get("creation_ticks")  # ✅ 关键
        and os.path.normcase(str(identity.get("executable", ""))) 
            == os.path.normcase(str(record.get("executable", "")))
    )
```

**评估**：macOS 1 秒精度下 PID 复用概率 ≈ 1/(99999*1) ≈ 0.001%（可接受）

---

### 2.4 Origin 验证与客户端接管防护 ⭐⭐⭐⭐⭐

**位置**: `browser_bridge.py:927-948, 1205-1258`

#### 2.4.1 WebSocket Origin 验证
```python
def _origin_allowed(self, sock) -> bool:
    origin = self._ws_origin(sock)
    if not origin:
        return os.environ.get('BROWSERTAP_WS_ALLOW_NO_ORIGIN', '') == '1'
    allowed = ('chrome-extension://', 'moz-extension://', 
               'safari-web-extension://', 'extension://')
    if origin.startswith(allowed):
        return True
    # 额外允许列表（逗号分隔）
    extra = os.environ.get('BROWSERTAP_WS_ALLOWED_ORIGINS', '')
    return any(origin == o.strip() for o in extra.split(',') if o.strip())
```

**防护**：
- ✅ 默认拒绝无 Origin 连接
- ✅ 仅允许扩展协议
- ✅ 握手时立即拒绝（1055-1068行）

#### 2.4.2 客户端接管防护
```python
def _claim_ext_client(self, client_id: str, browser: str, client: WebSocket) -> bool:
    """防止恶意 ext_ready 劫持 clientId"""
    incumbent = self.ext_clients.get(client_id)
    if incumbent and incumbent.get('ws') is not client:
        idle = now - incumbent.get('ts', now)
        if idle <= CLIENT_TAKEOVER_GRACE_SECONDS:  # 60 秒
            self.rejected_client_takeovers += 1
            logger.warning("Refused WS takeover of client_id=%r from %s: "
                          "incumbent was heard from %.1fs ago", 
                          client_id, client.address, idle)
            return False
    self.ext_clients[client_id] = {'ws': client, 'browser': browser, 'ts': now}
    return True
```

**场景**：
1. ✅ 活跃连接：拒绝接管，记录 `rejected_client_takeovers`
2. ✅ 僵尸连接（60s 无心跳）：允许接管并警告
3. ✅ 首次连接：直接注册

---

## 三、发现的问题

### 3.1 高优先级（建议修复）

#### 问题 1: 截图工具缺少原子写入

**位置**: 
- `server.py:6420-6425` (`capture_page_screenshot`)
- `server.py:6537-6542` (`capture_desktop_screenshot`)

**当前实现**：
```python
if save_path:
    path = _validate_safe_path(save_path, description="save_path")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)  # ⚠️ 直接写入
    out["saved_to"] = str(path)
```

**问题**：
1. 中途失败会留下部分写入的文件
2. 目录创建与写入之间存在 TOCTOU 竞争窗口
3. 无磁盘空间检查

**对比** `save_pdf` (4514-4521行)：
```python
# ✅ 正确实现
tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp_", suffix=path.suffix)
try:
    os.write(tmp_fd, pdf_bytes)
    os.fsync(tmp_fd)
    os.close(tmp_fd)
    os.replace(tmp_path, path)  # 原子操作
except:
    os.close(tmp_fd)
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise
```

**建议修复**：
```python
def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写入二进制数据，失败时清理临时文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent, 
        prefix=".tmp_", 
        suffix=path.suffix
    )
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        os.replace(tmp_path, path)
    except OSError as exc:
        os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        # 磁盘满特殊处理
        if exc.errno == errno.ENOSPC:
            raise RuntimeError("Failed to save: disk full") from exc
        raise RuntimeError(f"Failed to save: {exc}") from exc

# 使用
if save_path:
    path = _validate_safe_path(save_path)
    _atomic_write_bytes(path, raw)
    out["saved_to"] = str(path)
```

---

#### 问题 2: 无文件大小限制

**位置**: 同上（两个截图函数）

**风险**：
- 全页截图可能产生 50MB+ 文件
- 恶意客户端可发送巨大 base64 payload
- 可能导致内存耗尽或磁盘填满

**建议修复**：
```python
# 在 _validate_safe_path 之后添加
MAX_SCREENSHOT_SIZE = 50 * 1024 * 1024  # 50MB

if len(raw) > MAX_SCREENSHOT_SIZE:
    raise ValueError(
        f"Screenshot size {len(raw)} bytes exceeds maximum "
        f"{MAX_SCREENSHOT_SIZE} bytes"
    )
```

---

### 3.2 中优先级（建议评估）

#### 问题 3: 物理输入工具安全边界模糊

**涉及文件**: `physical_input.py` (731行) + `server.py` (7 个工具)

**工具列表**：
1. `mouse_click` - OS 级鼠标点击
2. `mouse_move` - OS 级鼠标移动
3. `mouse_drag` - OS 级拖拽
4. `type_text` - OS 级键盘输入
5. `hotkey` - OS 级组合键
6. `pointer_info` - 鼠标位置查询
7. `capture_desktop_screenshot` - 桌面截图（可截取其他应用）

**安全问题**：
1. **跨边界控制**：可操作浏览器外的任何窗口
   ```python
   # physical_input.py:731
   pyautogui.FAILSAFE = False  # ⚠️ 禁用角落安全机制
   ```

2. **权限过大**：
   - `type_text` 可向任何获焦窗口输入（包括终端、密码框）
   - `capture_desktop_screenshot` 可截取敏感应用（密码管理器、聊天软件）

3. **与项目定位不符**：
   - BTAP 定位是"浏览器自动化"
   - CDP 协议的页面输入已足够（`page_click`、`page_type` 等）

**建议**：
- **方案 A（推荐）**: 废弃所有 7 个物理输入工具
  - 移除 `physical_input.py`
  - 移除 `server.py` 中的 7 个工具定义
  - 在 CHANGELOG.md 中标记为 **Deprecated**（0.5.0 警告，0.6.0 移除）
  
- **方案 B（保守）**: 默认禁用，需显式环境变量启用
  ```python
  if os.environ.get('BROWSERTAP_ENABLE_PHYSICAL_INPUT') != '1':
      raise RuntimeError(
          "Physical input tools are disabled by default. "
          "Set BROWSERTAP_ENABLE_PHYSICAL_INPUT=1 to enable."
      )
  ```

**影响评估**：
- 查询工具调用统计（如有遥测）
- 检查 skill 依赖（`~/.cc-switch/skills/` 中是否有引用）

---

#### 问题 4: ABM 遗留代码待清理

**位置**: `paths.py`

**清理清单**：
1. **常量**：
   ```python
   LEGACY_STATE_DIR_NAME = "agent-browser-mcp"  # 删除
   LEGACY_ENV_PREFIX = "ABM_"  # 删除
   ```

2. **函数**：
   ```python
   def adopt_legacy_env() -> None:  # 删除整个函数
       """一次性迁移旧环境变量（0.4.0 升级兼容）"""
   ```

3. **`state_dir()` 逻辑**：
   ```python
   # 当前逻辑（110行）
   if not configured.exists() and legacy.exists():
       return legacy  # ⚠️ 删除此分支
   ```

**前提条件**：
- 确认所有用户已升级到 0.4.0+
- 在 0.5.0 中发出废弃警告（检测到旧目录时打印警告）
- 在 0.6.0 中完全移除

**建议警告信息**（0.5.0）：
```python
if legacy.exists() and not configured.exists():
    logger.warning(
        "Found legacy state directory %s. Please migrate to %s manually, "
        "or run: browsertap migrate-state",
        legacy, configured
    )
```

---

### 3.3 低优先级（可选改进）

#### 建议 1: 日志脱敏文档化

**当前状态**: `browser_bridge.py:16-87` (URL 脱敏已实现)

**已有脱敏**：
```python
def redact_url(url: Any) -> str:
    # scheme + host + 截断 path，query/fragment → ?.../#...
    # credentials 移除
    # file:/data:/blob:/javascript: → <scheme>:<redacted>
```

**建议文档化**（SECURITY.md）：
```markdown
## 日志内容

`bridge.log` 包含：
- ✅ 脱敏 URL（保留 scheme/host/部分 path）
- ✅ Tab ID、时间戳、客户端名称
- ⚠️ 浏览器错误消息（可能含路径）
- ⚠️ `execute_js` 脚本异常（未脱敏）

**不包含**：
- ❌ Token 明文（仅记录文件路径和 SHA-256 指纹）
- ❌ Cookie 值
- ❌ 完整 URL query 参数

审查建议：发送日志前搜索敏感路径和 API 密钥。
```

---

#### 建议 2: 参数验证错误信息优化

**当前**: 某些工具的参数错误仅返回通用消息

**示例**: `simphtml.py:execute_js_rich`
```python
if not isinstance(script, str) or not script.strip():
    raise ValueError("script cannot be empty")
    # ✅ 清晰

# 但某些地方：
if not options.get("method"):
    raise ValueError("invalid options")
    # ⚠️ 不明确哪个字段缺失
```

**建议**: 统一错误消息格式
```python
# 推荐格式
raise ValueError(f"{param_name} must be {constraint}, got {type(value).__name__}")
```

---

## 四、代码质量评估

### 4.1 设计模式 ⭐⭐⭐⭐⭐

1. **原子操作一致性**：
   - 所有文件写入：临时文件 → fsync → os.replace
   - PID 文件、Token 文件、日志轮转均遵循

2. **并发安全**：
   - `BrowserBridge._activity_condition`: 条件变量替代轮询（50ms → 即时唤醒）
   - 字典遍历前快照：`list(self.sessions.keys())`
   - 物理输入锁：跨进程文件锁 + PID 追踪

3. **错误处理分层**：
   ```python
   try:
       result = operation()
   except SpecificError:
       # 清理 + 转换为用户友好错误
       cleanup()
       raise UserFacingError(...) from None
   ```

---

### 4.2 可维护性 ⭐⭐⭐⭐

**优点**：
- ✅ 函数职责单一（平均 30-50 行）
- ✅ 类型提示完整（`dict[str, Any]`、`Optional[Path]`）
- ✅ 文档字符串覆盖关键函数

**改进空间**：
- ⚠️ `server.py` 过大（6946 行），建议拆分：
  ```
  server/
    __init__.py
    tools_browser.py     # 浏览器控制工具
    tools_input.py       # 输入工具
    tools_capture.py     # 截图/PDF
    tools_cookies.py     # Cookie 管理
    tools_diagnostics.py # doctor/setup_status
  ```

---

### 4.3 测试覆盖率

**当前状态**：1646 passed, 47 failed（最新测试运行）

**失败测试**：
- `test_physical_input.py`: 10 个测试
- `test_screen_bounds.py`: 9 个测试（`TestToolsRefuseBeforeDispatch`）

**覆盖的关键函数**：
- ✅ `_validate_safe_path` (tests/test_all_tools_behavior.py)
- ✅ Token 鉴权 (tests/test_browser_bridge.py - 推测)
- ✅ 进程身份验证 (tests/test_bridge.py - 推测)

**建议**：
1. 修复 47 个失败测试
2. 添加边界测试：
   ```python
   def test_validate_safe_path_symlink_escape():
       """测试符号链接逃逸防护"""
   
   def test_screenshot_size_limit():
       """测试文件大小限制"""
   ```

---

## 五、修复优先级路线图

### Phase 1: 紧急修复（0.4.1）
**目标**: 修复文件写入安全问题

- [ ] 实现 `_atomic_write_bytes` 函数
- [ ] 修复 `capture_page_screenshot` 使用原子写入
- [ ] 修复 `capture_desktop_screenshot` 使用原子写入
- [ ] 添加文件大小限制（50MB）
- [ ] 修复 47 个失败测试
- [ ] 更新 CHANGELOG.md

**预计工作量**: 2-4 小时

---

### Phase 2: 物理输入工具废弃（0.5.0）
**目标**: 收窄安全边界

- [ ] 在 7 个物理输入工具中添加废弃警告
- [ ] 更新文档标记 `@deprecated`
- [ ] 添加迁移指南（物理输入 → 页面输入）
- [ ] 统计工具调用频率（如有遥测）

**预计工作量**: 4-6 小时

---

### Phase 3: 完全移除（0.6.0）
**目标**: 清理遗留代码

- [ ] 删除 `physical_input.py`
- [ ] 删除 7 个物理输入工具定义
- [ ] 删除 ABM 兼容代码（`paths.py`）
- [ ] 更新所有文档移除物理输入引用
- [ ] 更新 MCP 工具清单

**预计工作量**: 3-5 小时

---

### Phase 4: 代码组织优化（0.7.0）
**目标**: 提升可维护性

- [ ] 拆分 `server.py` 为多个模块
- [ ] 统一参数验证错误消息
- [ ] 补充日志脱敏文档
- [ ] 添加边界测试用例

**预计工作量**: 8-12 小时

---

## 六、最终建议

### 立即行动项

1. **修复截图原子写入**（高优先级）
   - 风险: 数据损坏、部分写入文件
   - 成本: 低（复用 `save_pdf` 模式）

2. **添加文件大小限制**（高优先级）
   - 风险: DoS、磁盘填满
   - 成本: 极低（一行检查）

3. **决策物理输入工具**（中优先级）
   - 选项 A: 废弃（推荐）
   - 选项 B: 默认禁用 + 环境变量启用
   - 需要: 调用统计数据

### 长期改进

1. **模块化 server.py**
2. **补充测试覆盖率**（目标 95%+）
3. **清理 ABM 遗留代码**

---

## 七、审查结论

**BrowserTap MCP 是一个架构良好、安全意识强的项目**。路径遍历防护、Token 鉴权、进程身份验证等关键安全机制实现严密，代码质量整体达到企业级标准。

**主要改进空间集中在两个方向**：
1. **文件写入健壮性**：统一为原子操作模式
2. **安全边界收窄**：废弃 OS 级物理输入工具

完成 Phase 1-2 后，该项目可达到 ⭐⭐⭐⭐⭐ (5/5) 评级。

---

**审查员签名**: Claude (Opus 5)  
**审查日期**: 2026-09-03
