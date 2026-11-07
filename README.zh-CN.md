# claude-lamp-windows

[English](README.md)

把 [Moonside](https://developer.moonside.design/) 灯变成 Windows 上 [Claude Code](https://code.claude.com/docs/en/overview) 的状态灯。一个小型后台程序跟随这台电脑上所有 Claude Code 会话，把结果显示在一盏灯上，抬头看一眼就知道有没有事情等你。

| 灯 | 含义 |
|---|---|
| 粉色常亮 | 至少一个会话卡在等你：权限弹窗或问题弹窗 |
| 蓝色慢波浪 | 至少一个会话在工作，没有会话卡住 |
| 暖白 | 所有会话都停了 |
| 灭 | 连续 30 分钟没有会话工作；一旦有会话开始工作或需要你，灯就会亮 |

这是非官方项目，和 Moonside、Anthropic 都没有关系。

## 和 claude-lamp 的区别

[claude-lamp](https://github.com/bobek-balinek/claude-lamp) 在 macOS 上做了这件事，本项目的想法来自它。本项目是另写的版本，没有共用它的代码：

- **Windows：** 后台程序通过 Windows 任务计划程序里当前用户的任务运行。
- **不用 hook：** claude-lamp 注册了 Claude Code hook，包括每次工具调用之前的一个。本项目读取 Claude Code 自己保存在 `~/.claude/sessions` 的状态文件，所以 `settings.json` 不用改，会话里不会多运行任何东西，灯出问题也影响不到会话。
- **同时看多个会话：** 灯显示所有会话里最紧急的状态，顺序是卡住、工作、空闲。
- **夜间时段：** 可以设一个时间段，电脑没人用时灯自动熄灭。

## 安装

需要：支持低功耗蓝牙的 Windows、Git、Python 3.10 或更新版本、Claude Code、一盏 Moonside 灯。

**1. 安装。**

```powershell
git clone https://github.com/fushanbobfan/claude-lamp-windows
cd claude-lamp-windows
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

**2. 让灯空出来。** 灯被任何设备连着时，蓝牙搜索看不到它，所以手机 App 连着灯时电脑搜不到。把 Moonside App 从后台划掉，或者临时关一下手机蓝牙。

**3. 试一下。** `scan` 列出附近的灯，不发送任何指令。`demo` 把三种灯效各放 8 秒。

```powershell
.venv\Scripts\python moonside_lamp.py scan
.venv\Scripts\python moonside_lamp.py demo
```

**4. 设成每次登录时启动。** 这一步为当前用户注册名为 `moonside-lamp` 的计划任务，并立刻启动。

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1
```

撤销方法：运行下面的命令，再删掉文件夹。

```powershell
Stop-ScheduledTask -TaskName moonside-lamp; Unregister-ScheduledTask -TaskName moonside-lamp -Confirm:$false
```

## 日常使用

命令格式是 `.venv\Scripts\python moonside_lamp.py <命令>`。

| 命令 | 作用 |
|---|---|
| `status` | 列出卡住和在工作的会话，并显示灯的状态 |
| `off` | 熄灯并断开连接，让手机 App 可以连上；重启后仍然有效，直到运行 `on` |
| `on` | 重新跟随 Claude Code |
| `quiet 01:00 10:00` | 设置夜间时段（本地时间）：在这段时间内，键盘鼠标 15 分钟没有操作后，灯熄灭（粉色也不亮），后台程序断开连接；一有操作就恢复。加 `--idle 0` 则不管电脑有没有人用，整段时间都保持熄灭。`quiet off` 取消 |
| `pin <地址>` | 只用这盏灯，地址是 `scan` 显示的那个，从后台程序下一次连接时生效；想马上生效就重启任务。`pin off` 恢复默认，连蓝牙信号最强的灯 |
| `demo "<指令>" ...` | 把你指定的灯光指令各放 8 秒；先运行 `off` |

想改灯效，就按[协议页](https://developer.moonside.design/)里的指令修改 `moonside_lamp.py` 开头的 `LOOKS`，然后重启任务：

```powershell
Stop-ScheduledTask -TaskName moonside-lamp; Start-ScheduledTask -TaskName moonside-lamp
```

## 使用前要知道的事

- **手机 App 和后台程序只能轮流用灯：** 后台程序连着灯时，App 看不到灯。用 App 之前先运行 `off`；运行 `on` 之前把 App 划掉。
- **粉色表示有会话卡在等你：** 会话回复完、在文字里问你问题时，显示为空闲，因为它并没有卡住。
- **灯里保存的颜色或效果会被替换：** 想要回来，在 App 里重新选。
- **亮度由你决定：** 后台程序不发送亮度指令，亮度用灯自己的控制方式调。
- **只统计 Windows 上的 Claude Code：** 其他编程代理不影响灯；WSL 里的会话把文件存在 Linux 那边，后台程序看不到。Claude Code 的后台会话在进程运行期间才算。如果你设置了 `CLAUDE_CONFIG_DIR`，后台程序会读那个文件夹。
- **状态文件没有官方文档，** 以后的 Claude Code 可能会改。如果这些文件全都不再包含 `status` 字段，后台程序会记录警告，并改用 `claude agents --json`（Claude Code [官方文档写明](https://code.claude.com/docs/en/agent-view)的外部读取会话状态的命令），每 15 秒查询一次。
- **后台程序停止时，灯保持原样：** 电脑睡眠、关机或停止任务都算。灯自己保存状态，所以它应该会保持最后的样子，直到后台程序重新运行。

## 工作原理

后台程序每秒读一次 `~/.claude/sessions/*.json`，忽略已退出的进程，取最紧急的状态。状态变化要等 3 秒，用来忽略闪动；粉色例外，立刻显示。指令是通过低功耗蓝牙写入的纯 ASCII 文本，只在状态变化时发送，因为任何写入都会打断正在播放的动画。灯不返回任何信息，所以每条指令发送前都先对照[协议页](https://developer.moonside.design/)的指令表检查。

同时有 38 个会话、以 Windows 的“低于正常”进程优先级运行时，实测占一个 CPU 核心不到 1%，内存不到 30 MB。日志在 `state/lamp.log`。

测试环境：`MOONSIDE-L1` 灯（固件 1.0）、Windows 11、Python 3.14、桌面版 Claude Code 2.1.317。协议页只有一套面向 Moonside 灯的指令，其他型号没有测试过。

## 测试

```powershell
.venv\Scripts\python -X utf8 -m unittest -v test_moonside_lamp
```

需要 Claude Code 在本机运行的测试，在没有运行时会自动跳过。

## 致谢

- [claude-lamp](https://github.com/bobek-balinek/claude-lamp)（MIT）：想法来自这里。
- [moonside-ha](https://github.com/diotav/moonside-ha)（MIT）：实测发现，灯亮着时应用动画会和当前显示的内容混在一起，所以动画要在关灯指令 `LEDOFF` 之后发送。

## 许可证

MIT
