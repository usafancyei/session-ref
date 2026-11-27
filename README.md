# session-ref

在 Codex CLI、Claude Code 和 agy CLI 草稿中先按半角空格，再按 `#`，当前终端会显示可搜索的会话列表。选中后，完整会话 ID 会插入空格后面。本机终端和 SSH 登录 Mac 使用同一套按键，选择器直接读写当前 PTY。

## 构建和试用

在运行 CLI 的 Mac 上准备 zsh 和 Python 3.11 或更高版本。需要使用的 CLI 应已在当前 `PATH` 中可用。在本项目根目录执行以下命令。

```sh
make setup
./session-ref list --summary
./session-ref run claude
```

`make setup` 在项目的 `.venv/` 安装 Claude Agent SDK。终端列表使用 Python 标准库。`./session-ref list --source codex` 显示单个来源的会话标题、目录和更新时间，`./session-ref list --by-project` 按项目分组。

## 在现有终端接入

在 Terminal 或 iTerm2 的 zsh 中运行 `source ./shell/session-ref.zsh`，再照常启动 `codex`、`claude` 或 `agy`。已经运行的 CLI 可在重新启动后用官方 resume 功能恢复会话。IDE 中从 zsh 启动的集成终端也使用同一入口。

如需在新窗口自动加载，把下面这一行的路径换成项目实际位置，加入 `~/.zshrc`。

```zsh
source "/absolute/path/to/session-ref/shell/session-ref.zsh"
```

每次启动时按当前 `PATH` 解析原命令，因此可以继续使用已有的隔离启动器和官方升级命令。使用 `command claude` 可直接启动原 CLI。移除加载行并新开 zsh 即可解除接入。

## 通过 SSH 登录 Mac

先用 `ssh 用户名@Mac主机` 登录，在远端 zsh 中加载远端项目的 `shell/session-ref.zsh`，然后照常运行三个 CLI。列表读取该 Mac 上当前用户的会话，在 SSH 窗口中显示。也可以从客户端直接指定远端启动命令（示例路径按实际位置替换）。

```sh
ssh -t user@mac '~/codexProject/session-ref/session-ref run codex'
```

`-t` 为远端命令分配交互式终端。只需查看列表时可执行 `ssh user@mac '~/codexProject/session-ref/session-ref list'`。

## 选择会话

- 连续手打空格和 `#` 打开列表。单独输入 `#` 会保留在草稿中。可按工具名、标题、项目路径或部分 ID 搜索。多个关键词用空格分隔。退格删除一个字符，Ctrl+U 清空搜索。
- 按 Tab 切换时间视图和项目视图。切换时保留搜索词和选中的会话。项目视图中，当前项目排在最前，其他项目按组内最近一次更新排列，每组内从新到旧。
- 按 ↑ 或 ↓ 移动，Page Up / Page Down 翻页，按 Enter 插入选中 ID。原草稿仍可编辑，发送由你决定。
- 按 Esc 取消，草稿中会保留刚输入的空格和 `#`。普通 `@` 交给原 CLI。
- Terminal 和 iTerm2 通过 bracketed paste 标记粘贴内容，带 `#` 的粘贴片段会原样通过。自定义按键映射若把粘贴模拟成逐字键入，PTY 收到的就是按键。
- 设置 `export SESSION_REF_TRIGGER='%'` 可用单个 `%` 触发，设置为 `' %'` 则需先按半角空格。

## 在同一窗口切换 CLI

在本机终端或 SSH 窗口中，按 `Ctrl+\` 切到上一个 CLI，按 `Ctrl+/` 沿同一顺序切到下一个。比如依次打开 Codex、Claude、agy 后，从 agy 按 `Ctrl+/` 会绕回 Codex，再按一次到 Claude。此时按 `Ctrl+\` 就回到 Codex。只有一个 CLI 时，两个键都会停在原处。

输入或粘贴草稿、打开选择器或按名称切换后，下次切换会按最近使用顺序重新开始。

有些终端会把 `Ctrl+/` 和 `Ctrl+_` 发成同一个字节。只有一个可用 CLI 时，这个字节会交给它处理，可用于撤销。有多个可用 CLI 时，两个键都会切到下一个。如果终端能分别上报这两个键，`Ctrl+_` 始终交给当前 CLI。

如果在 Windows 的 VS Code 集成终端里按 `Ctrl+\` 打开了拆分编辑器，可在 `terminal.integrated.commandsToSkipShell` 列表中加入 `"-workbench.action.splitEditor"`，让终端接收这个键。[VS Code 终端快捷键设置](https://code.visualstudio.com/docs/terminal/advanced#_keyboard-shortcuts-and-the-shell)

从接入了 `session-ref` 的 zsh 启动任意一个 CLI。手打一个半角空格，再按反斜杠 `\`，输入目标名称后按 Enter。三个命令在 Codex、Claude 和 agy 中都可以使用。

| 手打内容（前面加一个半角空格） | Enter 后显示 |
| --- | --- |
| `\claude` | Claude Code |
| `\agy` | agy |
| `\codex` | Codex |

第一次切到某个 CLI 会在本次启动的项目目录中打开它。再次切回会接上同一个进程，看到它的最新输出，可以继续编辑原来的草稿。确认切换的 Enter 由 `session-ref` 接收。后台任务继续运行，需要确认时会等待你切回。

快速连按空格和反斜杠时，两键都用来打开切换框。单独按空格后等约 150 毫秒，当前 CLI 就会收到这个空格，可用于勾选或翻页。下一键仍可用反斜杠打开切换框，已经送出的空格会保留。

在切换框里按 Ctrl+U 清空名称，按 Esc 取消。输入普通反斜杠文本（如「空格 + `\path`」）时，一旦名称不再匹配就会原样写回草稿。终端标记的整段粘贴会直接进入当前 CLI。若把会话选择器显式配置为相同的「空格 + 反斜杠」，这个组合仍打开会话列表。

每个终端窗口独立管理自己启动的 CLI。如果后台程序需要查询当前光标位置，会在切回时收到终端应答。正常退出当前 CLI 后，会回到最近使用且仍在运行的那个。最后一个 CLI 退出后返回 shell。关闭整个窗口或断开 SSH 会结束这组进程。

## 会话来源

| 来源 | 列表接口与字段 |
| --- | --- |
| Codex | `codex app-server` 的 `thread/list`，分页读取活动和归档的交互会话。优先显示名称，缺失时使用预览。[App Server 文档](https://developers.openai.com/codex/app-server/) |
| Claude Code | 独立虚拟环境里的 `claude_agent_sdk.list_sessions()` 跨项目枚举，自定义标题优先于摘要。[Agent SDK 文档](https://code.claude.com/docs/en/agent-sdk/python#list_sessions) |
| agy | `agy acp` 初始化后检查能力，再通过 `session/list` 分页。无标题记录保留目录、时间及 ID。[ACP 文档](https://agentclientprotocol.com/protocol/v1/session-list) |

每个来源最多显示 5 条。当前项目的匹配会话优先占用名额，剩余名额按更新时间补足。时间视图先列当前项目，再列其他项目，两部分都按更新时间从新到旧排列。两个视图共享同一批搜索结果。列表隐藏会话 ID，按 Enter 时插入完整 ID。`list --summary` 统计各来源的全部会话。

Git 项目以最近的工作树根目录识别，在仓库的不同子目录启动 CLI 会归到同一项目。普通目录以解析符号链接后的完整路径识别。Codex 指定 `-C` 或 `--cd` 时使用指定目录。

某个来源失败时，列表底部会显示原因，其他结果仍可选择。元数据通过本地管道传递，选择器仅把 ID 写回发起选择的 PTY。

## 验证

`make test` 运行协议、按键和 PTY 测试，包含 zsh 原命令、三个 CLI 与三个来源的九种组合和并发终端。集成用例驱动真实终端列表，使用隔离的 CLI 与测试会话。

手工验收时，可在本机终端和交互式 SSH 会话中分别启动 CLI。在中文草稿中间打开列表，选中后继续输入，并检查 Esc、普通 `@`、真实粘贴和窗口缩放。用 `./session-ref list --summary` 可单独复核官方数据源。
