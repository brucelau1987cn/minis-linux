# Minis Linux

**部署在自己 Linux 主机上的个人 AI 助手。**

从 Telegram 私聊或终端发消息，让 Minis 整理值班记录、起草周报，或按已安装的技能处理工作区里的资料。会话保存在运行 Minis 的主机上，重启后可以继续。模型由你选择：接入 CLIProxyAPI（CPA）或其他支持工具调用的 OpenAI 兼容服务。

[下载稳定版](https://github.com/brucelau1987cn/minis-linux/releases/latest) · [功能说明](docs/features.md) · [部署手册](systemd/README.md)

## 主要功能

- **Telegram 私聊。** 用同一个窗口聊天、调用技能和管理记忆。只接收配置中指定账号的私聊，不需要开放入站端口。
- **任务进度与停止（v0.3 起）。** 发任务先收到回执，`/status` 查看当前阶段。在 Minis 开始保存和发送结果前，可用 `/stop` 放弃本轮。Minis 不保存这轮对话，也不发送迟到结果；已发往模型服务的请求可能继续执行。
- **终端对话。** 输入 `minis` 进入行式交互界面。终端与 Telegram 分别保存会话、记忆和模型配置。
- **技能与工作区。** Minis 按需读取本机安装的 `SKILL.md`。仓库自带周报技能；Telegram 和交互式终端只能列出和读取专用工作区顶层的文本文件，不执行技能脚本。
- **可管理的记忆。** 发送「记住：周报用中文」保存一条偏好，再用「查看记忆」或「忘记第 1 条」管理。`/new` 清除本机对话上下文，保留长期记忆。
- **Linux 常驻与更新。** 安装向导配置 systemd 服务，服务以非 root 身份运行。`minis update` 校验稳定版本签名后再安装，保留配置、技能和聊天数据。

> Minis 在你的主机上保存状态并运行工具，模型推理仍由你配置的服务完成。对话、已保存的记忆和工具读取的内容可能发送给该服务。不要把密钥或敏感资料放进工作区、技能或记忆。

## 快速开始

### 不安装服务，先试终端

下载并解压[稳定版](https://github.com/brucelau1987cn/minis-linux/releases/latest)，在解压目录里运行：

```bash
python3 minis_cli.py --demo
```

用 `/exit` 退出。演示使用固定的本地响应，不需要 API Key，不访问网络，也不修改正式会话。

### 部署到 Linux

需要 **Python 3.10+、systemd 247+**、系统 CA 证书，以及到 Telegram 和模型服务的出站连接。先通过 `@BotFather` 创建专用 Bot，准备好自己的 Telegram 数字用户 ID 和模型 API Key。下载 ZIP 或 tar.gz，并按发布页的 `SHA256SUMS` 核对下载包。进入解压目录后运行：

```bash
sudo /usr/bin/python3 -EsB install.py
```

按向导填写 Bot Token、模型 Base URL 和模型 ID 等信息。随后在 Bot 私聊发送 `/status` 检查消息收发，再发一句普通消息验证模型。录制键盘输入的终端请使用[凭据文件方式](systemd/README.md#一键部署推荐)。

## 常用命令

```bash
minis                 # 打开终端对话
minis model           # 选择或添加网关，再设置终端默认模型（v0.3 起）
minis model --show    # 查看已保存的终端默认模型
minis --version       # 查看已安装版本
minis update --check  # 查找更新，不安装
minis update          # 验签并安装较新的稳定版
```

Telegram 聊天内可用 `/help` 查看命令，`/skills` 列出已装技能。更详细的使用示例与权限说明见[功能说明](docs/features.md)。

`minis model MODEL_ID` 可直接保存当前默认网关上的指定模型。终端对话中的 `/model` 则立即切换当前会话，不改默认设置。两种命令都不改 Telegram 服务的模型配置。

## 开发与验证

项目使用 Python 标准库，无需安装第三方 Python 包。离线测试和安装器检查不会部署服务：

```bash
python3 -m unittest -v
python3 -EsB install.py --check
```

Minis Linux 是独立实现，产品设计参考了 [OpenMinis](https://github.com/OpenMinis/OpenMinis) 的对话、技能和工作区思路，文档编写参考了 [Antigravity 功能总览](https://antigravity.google/docs/features/) 的分场景说明。它不是上述项目的官方移植，也不依赖 Hermes 或 OpenClaw 运行。
