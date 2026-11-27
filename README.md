# Minis Linux — CLI 与 Telegram 常驻助手

独立编写的 Linux 单用户 Agent，不是 OpenMinis 官方移植，也不依赖 Hermes 或 OpenClaw。Python 3.10+ 标准库即可运行。参考 OpenMinis 的「对话 + 工具 + 工作区」设计；没有复制其实现。

## Telegram 常驻模式

一个专用 Telegram Bot 私聊窗口，不用切换模式。Linux 用 systemd 常驻，不开入站端口，只接受指定的数字用户 ID。

- **直接对话：** 发消息就能多轮聊天；重启后可接着聊。
- **按需用技能：** 例如发「帮我整理一份周报」，模型可读取已安装的 `SKILL.md`，并使用只读工作区工具。发 `/skills` 查看已装技能。
- **长期记忆：** 发「记住：周报用中文」保存偏好；「查看记忆」「忘记第 1 条」可管理。`/new` 清空本机聊天上下文，不删除长期记忆。
- **任务进度：** 发消息先收到回执，`/status` 查看当前阶段，`/stop` 停止尚未提交的任务。停止后不保存该轮，也不发送迟到结果；旧模型请求退出前不接新任务。

**一键安装**（Linux、systemd 247+、Python 3.10+）：先用 @BotFather 创建一个专用 Bot，然后在终端执行：

```bash
git clone https://github.com/brucelau1987cn/minis-linux.git && cd minis-linux && sudo /usr/bin/python3 -EsB install.py
```

按向导填写 Telegram 数字 ID、Bot Token 和模型配置。安装完成后，在 Bot 私聊发 `/status`，再发一句话验证模型。服务器无需 GitHub Token。若 sudo 或堡垒机录制键盘输入，请用安装手册里的凭据文件方式，不要在向导里输入密钥。

## 终端对话与版本更新

安装后可以从任意目录运行：

```bash
minis                 # 打开交互式终端对话（行式界面）
minis --demo          # 不联网、不用密钥，体验终端对话
minis --version       # 显示当前版本
minis update --check  # 只检查是否有新稳定版本
minis update          # 安装新稳定版本，会请求 sudo
```

终端和 Telegram 各有独立的会话、记忆和模型设置。首次打开终端对话时可填写模型配置，也可事先设置 `MINIS_BASE_URL`、`MINIS_MODEL`、`MINIS_API_KEY`。终端同样可以直接聊天、按需加载技能和管理记忆。

`minis update` 从本仓库选择更高的 `vX.Y.Z` 标签，跳过预发布版本，并用内置公钥验签后才安装。升级保留配置、凭据、技能和聊天数据；启动失败会尝试回退，安装被强制中断后可恢复。更多见 [systemd 安装手册](systemd/README.md)。只检查安装包可运行 `python3 install.py --check`，不启动服务。

## 30 秒离线体验

```bash
python3 minis.py --home ./demo-state --demo demo
# 输入 y，创建 demo-state/workspace/note.md；不确认则不写入。
python3 minis.py --home ./demo-state --history
python3 -m unittest -v
```

演示用固定的本地响应，不联网，不调用真实模型。重复确认创建同名文件也不会覆盖。

## 接入 CPA 或其他 OpenAI 兼容 API

在 Bash 里运行。把示例 URL 和模型 ID 换成你的实际值；不要填 CPA 管理密钥。

```bash
read -rsp 'API key: ' MINIS_API_KEY; printf '\n'
export MINIS_API_KEY
python3 minis.py --base-url https://api.example.com/v1 --model YOUR_MODEL_ID \
  --session notes '列出工作区的文件'
unset MINIS_API_KEY
```

调用 `/v1/chat/completions`。模型和网关需要支持标准工具调用；本地测试覆盖 HTTP 请求和工具结果回传，未连接真实 CPA。模型端只收到对话及已调用工具返回的内容，不会自动扫整台机器。使用同一个 `--session` 会在后续请求中重发该会话的历史，包括工具读出的文件内容；换模型或网关时使用新会话名，避免旧历史发给新服务。

## 已实现与边界

- `--session` 持久保存多轮对话；`--history` 输出选中会话的 JSON。会话记录独立，当前工作区仍由这些会话共享，不构成用户或会话隔离。
- 提供列文件、读 UTF-8 文本、创建新文件三种工具。工具只操作专用工作区顶层，拒绝路径穿越、符号链接及对既有文件的覆盖。审阅写入预览后明确确认才创建文件。除离线演示外，非交互运行拒绝写入。
- 建议普通 Linux 用户运行。默认工作区 `~/.local/share/minis-linux/workspace`，会话保存在同级 `sessions`，为明文。工作区不要放密钥或敏感文件，`--home` 应指定本人控制的私有目录。工作区限制不是 OS 级沙箱。
- Telegram 不开放 Shell、写文件或执行技能脚本。对话、长期记忆与工具结果可能发给模型；不要记入密码或密钥，也不要在工作区或技能中放敏感文件。

## 参考

[OpenMinis README](https://github.com/OpenMinis/OpenMinis/blob/1a71df0f381cefde80bf912b723de6f9831bc177/README.md) · [移动端构建说明](https://github.com/OpenMinis/OpenMinis/blob/1a71df0f381cefde80bf912b723de6f9831bc177/BUILDING.md)
