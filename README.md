# Minis Linux — CLI 原型

独立编写的 Linux 单用户前台 Agent，不是 OpenMinis 官方移植，也不依赖 Hermes 或 OpenClaw。Python 3.10+ 标准库即可运行。参考 OpenMinis 的「对话 + 工具 + 工作区」设计；没有复制其实现。

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
- 首版不含 Shell、任意命令执行、Web/Telegram、流式输出、后台服务、定时任务、长期记忆和 Skills 自动加载。未安装服务、未部署到任何机器。

## 参考

[OpenMinis README](https://github.com/OpenMinis/OpenMinis/blob/1a71df0f381cefde80bf912b723de6f9831bc177/README.md) · [移动端构建说明](https://github.com/OpenMinis/OpenMinis/blob/1a71df0f381cefde80bf912b723de6f9831bc177/BUILDING.md)
