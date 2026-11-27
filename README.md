# session-ref

在同一个终端窗口中，跨 **Codex CLI**、**Claude Code** 和 **agy CLI** 快速引用历史会话并无缝切换多个 CLI。

- **跨 CLI 引用会话**：在任意一个 CLI 的草稿中按 `空格 + #`，弹出可搜索的统一会话列表，选中后直接把完整会话 ID 插入草稿，方便让不同 AI 工具接力上下文。
- **单窗口多 CLI 切换**：在当前终端内按需拉起并在 Codex、Claude Code、agy 之间随时切换，后台进程持续运行，切回原进程时恢复近期画面并继续编辑未发送草稿。
- **零侵入接入**：通过 zsh 函数按当前 `PATH` 动态解析原命令，兼容已有隔离启动器和官方升级命令；非交互式命令与管道自动直通原 CLI。
- **本机与 SSH 一致**：直接读写当前 PTY，不依赖额外窗口管理器，在 Terminal、iTerm2、IDE 集成终端及 SSH 登录 Mac 时体验完全一致。

---

## 快速安装

### 环境要求

- **系统与 Shell**：macOS + `zsh`
- **Python**：3.11 或更高版本
- **AI CLI**：已在当前 `PATH` 中安装你要使用的 CLI（`codex`、`claude`、`agy` 中任意一个或多个即可）

### 1. 克隆项目并安装依赖

```sh
git clone https://github.com/usafancyei/session-ref.git
cd session-ref
make setup
```

`make setup` 会创建项目内独立虚拟环境 `.venv/` 并安装 Claude Agent SDK；终端界面与 PTY 控制仅使用 Python 标准库。

> 如果系统默认的 `python3` 低于 3.11，可显式指定解释器，例如 `make setup PYTHON=python3.12`。

### 2. 验证数据源

```sh
# 推荐先按已安装的来源单独验证（codex / claude / agy）
./session-ref list --source codex --summary

# 汇总全部来源的会话数（不打印标题或隐私内容）
./session-ref list --summary
```

全来源汇总时，如果有尚未安装的 CLI，标准错误会提示该来源找不到启动命令，整条命令也会返回退出码 `1`；这不影响已安装来源的正常使用。

### 3. 接入终端（zsh）

**临时试用**（仅当前终端窗口生效）：

```zsh
source ./shell/session-ref.zsh
```

**永久生效**（推荐）：将下面一行加入 `~/.zshrc`（替换为项目实际路径），然后执行 `source ~/.zshrc` 或新开终端：

```zsh
source "/path/to/session-ref/shell/session-ref.zsh"
```

配置完成后，直接在 Terminal、iTerm2 或 IDE 集成终端中照常运行 `codex`、`claude` 或 `agy` 即可；同时也会提供全局 `session-ref` 命令。

- **已在运行中的 CLI**：接入配置后重新启动，可用对应 CLI 的官方 `resume` 功能恢复原会话。
- **临时跳过包装**：运行 `command codex`、`command claude` 或 `command agy` 可直接启动原程序。
- **解除接入**：移除 `~/.zshrc` 中的 `source` 行并新开 zsh 窗口即可。

---

## 快捷键速查

| 场景 | 按键 / 输入 | 作用 |
| --- | --- | --- |
| 打开会话选择器 | `空格` + `#` | 弹出可搜索的跨 CLI 会话列表 |
| 选择器内搜索 | 直接输入（空格分多词） | 按工具名、标题、项目路径或部分 ID 过滤 |
| 选择器内编辑 | `Backspace` / `Option+Backspace`* / `Ctrl+U` | 删一字 / 删一词 / 清空搜索 |
| 选择器内浏览 | `↑` `↓` / `Page Up` `Page Down` / `Tab` | 移动 / 翻页 / 切换「时间视图」与「项目视图」 |
| 选择器确认 / 取消 | `Enter` / `Esc`（或 `Ctrl+C`） | 插入完整会话 ID / 取消并保留原输入 |
| 打开或按名切换 CLI | `空格` + `\codex` / `\claude` / `\agy` + `Enter` | 首次在当前项目启动，已启动则切回原进程 |
| 在已开 CLI 间轮换 | `Ctrl+\` / `Ctrl+/` | 切到上一个 / 下一个运行中的 CLI |

> \* `Option+Backspace` 删一词仅在发送传统 `Esc` 编码的终端中生效；在 CSI-u 键盘模式下可使用 `Ctrl+U` 清空搜索。

---

## 使用指南

### 1. 在草稿中引用历史会话

在通过 `session-ref` 启动的 `codex`、`claude` 或 `agy` 输入框中：

1. 连续手打 **半角空格 + `#`**，当前终端会弹出可搜索的会话列表。
2. 输入关键词过滤（支持工具名、会话标题、项目路径、部分 ID，多个词用空格分隔，也支持直接粘贴搜索词）。
3. 按 `↑` / `↓` 或 `Page Up` / `Page Down` 选中目标会话，按 `Tab` 可在 **时间视图** 和 **项目视图** 之间切换（保留搜索词和当前选中项）。
4. 按 `Enter` 将完整会话 ID 插入到刚才的空格后面，回到原草稿继续编辑，由你决定何时发送。

**防误触与取消规则**：

- 单独输入 `#`（前面没有刚打的空格）会直接保留在草稿中，不会弹出列表；普通 `@` 也照常交给原 CLI。
- 按 `Esc`（或 `Ctrl+C`）取消选择，草稿会保留刚输入的空格和 `#`。
- Terminal、iTerm2 等终端通过 bracketed paste 标记粘贴内容，包含 `#` 的粘贴片段会原样透传，不会误触发选择器。（若自定义按键映射把粘贴模拟为逐字键入，PTY 收到的是普通按键。）

### 2. 在同一终端窗口切换多个 CLI

#### 按名称拉起或切换

从接入了 `session-ref` 的终端启动任意一个 CLI 后，手打 **半角空格 + 反斜杠 `\`**，输入目标 CLI 名称并按 `Enter`：

| 输入（前面加一个半角空格） | 按 `Enter` 后切换至 |
| --- | --- |
| `\codex` | Codex |
| `\claude` | Claude Code |
| `\agy` | agy |

- **按需启动、后台保活**：首次切到某个 CLI 时，会在本次启动的项目目录中拉起它；再次切回时直接接管同一个进程，恢复近期画面并继续编辑未发草稿。切走后原 CLI 仍在后台继续运行，需要交互确认或终端应答时会等待你切回。
- **不干扰正常打字**：
  - 快速连按空格和 `\` 会直接打开切换框；单独按空格停顿约 150 毫秒后，空格会先送给当前 CLI（不影响空格翻页或勾选），随后再按 `\` 依然可以打开切换框，已送出的空格会保留。
  - 在切换框中可按 `Ctrl+U` 清空名称，按 `Esc` 取消；如果输入的内容不再匹配任何 CLI 名称（例如空格 + `\path`），会自动退出切换框并把原文本写回草稿。
  - 终端标记的整段粘贴会直接进入当前 CLI。

#### 用快捷键快速轮换

在已经打开多个 CLI 的窗口中，无需输入名称即可直接轮换：

- **`Ctrl+\`**：切到上一个 CLI
- **`Ctrl+/`**：切到下一个 CLI

例如依次打开 Codex、Claude、agy 后，从 agy 按 `Ctrl+/` 会绕回 Codex，再按一次到 Claude；此时按 `Ctrl+\` 会回到 Codex。当窗口内只有一个 CLI 时，这两个按键不会切换。开始输入/粘贴草稿、打开会话选择器或按名称切换后，下一次轮换会按最近使用顺序重新开始。

> **终端按键兼容提示**：
> - 部分传统终端会把 `Ctrl+/` 和 `Ctrl+_` 发成同一个字节。窗口里只有 1 个可用 CLI 时，该按键会透传给当前 CLI（可用于撤销）；有多个 CLI 时用于切到下一个。在能区分这两个键的终端里，`Ctrl+_` 始终交给当前 CLI。
> - 在 Windows 版 VS Code 集成终端（如远程 SSH 连 Mac）中，如果按 `Ctrl+\` 触发了“拆分编辑器”，可在 VS Code 设置 `terminal.integrated.commandsToSkipShell` 中添加 `"-workbench.action.splitEditor"`，将按键交给终端处理。详见 [VS Code 终端快捷键文档](https://code.visualstudio.com/docs/terminal/advanced#_keyboard-shortcuts-and-the-shell)。

#### 进程与窗口生命周期

- 每个终端窗口独立管理自己拉起的 CLI。
- 正常退出当前 CLI 后，会自动切回最近使用且仍在运行的 CLI；最后一个 CLI 退出后返回 shell。
- 关闭整个终端窗口或断开 SSH 连接，会一并结束该窗口启动的这组 CLI 进程。

### 3. 命令行直接调用

不想修改 `~/.zshrc`，或在脚本中单独查看会话列表时，可以直接调用 `session-ref`（未 source 时使用 `./session-ref`）：

```sh
# 直接启动单个交互式 CLI（原 CLI 的参数可照常附在后面）
session-ref run codex
session-ref run claude --resume
session-ref run agy

# 查看近期会话（当前项目优先，按更新时间倒序）
session-ref list

# 按项目分组查看
session-ref list --by-project

# 仅查看某个来源
session-ref list --source codex

# 仅输出各来源会话总数（不打印标题）
session-ref list --summary
```

> 非交互式用法（如 `--help`、`--version`、`codex exec`、`claude -p`、`agy --prompt` 或标准输入输出重定向）会自动直通给原 CLI，不会包裹 PTY。

### 4. 通过 SSH 登录 Mac 使用

在远端 Mac 上完成安装并配置 `~/.zshrc` 后，通过 `ssh user@mac` 登录，照常运行 `codex`、`claude`、`agy` 即可。会话列表会读取远端 Mac 上当前用户的会话，直接在当前 SSH 窗口中显示和操作。

也可以从客户端用单条命令直接启动远端 CLI 或查看列表（按实际仓库路径替换 `~/session-ref`）：

```sh
# -t 为远端交互式 CLI 分配伪终端
ssh -t user@mac '~/session-ref/session-ref run codex'

# 仅查看远端会话列表
ssh user@mac '~/session-ref/session-ref list'
```

---

## 自定义配置

- **修改会话选择器触发键**：通过环境变量 `SESSION_REF_TRIGGER` 指定，需为单个可见 ASCII 字符，前面可选加一个半角空格（默认为 `' #'`）。

  ```zsh
  # 改为单独按 % 触发
  export SESSION_REF_TRIGGER='%'

  # 改为先按半角空格，再按 % 触发
  export SESSION_REF_TRIGGER=' %'
  ```

  > 如果把触发键显式设为 `export SESSION_REF_TRIGGER=' \'`（单引号内为一个半角空格加一个反斜杠），该组合会优先用于打开会话选择器，而非 CLI 切换框。

- **关闭终端着色**：设置 `NO_COLOR=1` 即可禁用选择器颜色样式。

---

## 会话来源与排序规则

| 来源 | 数据接口与显示字段 | 参考文档 |
| --- | --- | --- |
| **Codex** | `codex app-server` 的 `thread/list`，分页读取活动和归档会话；优先显示名称，缺失时显示预览 | [App Server 文档](https://developers.openai.com/codex/app-server/) |
| **Claude Code** | 独立 `.venv/` 中的 `claude_agent_sdk.list_sessions()` 跨项目枚举；自定义标题优先于摘要 | [Agent SDK 文档](https://code.claude.com/docs/en/agent-sdk/python#list_sessions) |
| **agy** | `agy acp` 初始化校验能力后通过 `session/list` 分页；无标题记录保留目录、时间与 ID | [ACP 文档](https://agentclientprotocol.com/protocol/v1/session-list) |

- **数量与排序**：选择器（以及 `list`）每个来源最多展示 **5 条**。当前项目的匹配会话优先占用名额，剩余名额按更新时间倒序补足。**时间视图**先列当前项目、再列其他项目；**项目视图**将当前项目置顶，其余项目按组内最新更新时间排序，组内从新到旧。`list --summary` 统计各来源的全部会话。
- **项目归组**：Git 仓库按最近的 `.git` 根目录识别（在同一仓库的不同子目录启动都会归为同一项目）；普通目录按解析符号链接后的完整路径识别；Codex 指定 `-C` 或 `--cd` 时以指定目录为准。
- **容错与安全**：某个来源未安装或读取失败时，列表底部会提示原因，不影响其余来源正常选择。列表默认隐藏会话 ID，元数据仅通过本地管道传递，按 `Enter` 时校验 ID 格式后仅写回发起选择的 PTY。

---

## 开发与测试

```sh
make test
```

`make test` 运行标准库 `unittest` 套件，覆盖协议解析、分片转义序列、参数转发、zsh 入口、三个 CLI 与三个来源的组合及并发终端场景。

如果改动了输入处理或全屏恢复逻辑，建议在本机终端和交互式 SSH 会话中补充手工验证：在中文草稿中间打开列表并插入后继续编辑、测试 `Esc` 取消、普通 `@`、真实粘贴、窗口缩放以及多 CLI 切换，并用 `./session-ref list --summary` 复核数据源。
