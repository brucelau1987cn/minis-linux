# Telegram 常驻部署（systemd）

需要 Linux、systemd 247+、Python 3.10+、系统 CA 证书，以及到 Telegram Bot API 和模型网关的出站连接。**不用开端口，不要复用 Hermes 或其他程序正在使用的 Bot Token。** 先在 @BotFather 创建专用 bot，关闭加入群组的权限；拿到自己的 Telegram 数字用户 ID（不是 @用户名）。

## 1. 安装应用和配置

在本仓库目录运行。脚本和服务文件由 root 安装；服务进程使用 systemd 分配的非 root 动态用户。下方 `install ... telegram.conf` 只在首次安装时运行，升级时保留现有配置和密钥。

```bash
sudo install -d -m 0755 /opt/minis-linux
sudo install -m 0644 minis.py minis_telegram.py /opt/minis-linux/
sudo install -d -m 0700 /etc/minis-linux
sudo install -m 0600 systemd/telegram.conf.example /etc/minis-linux/telegram.conf
sudoedit /etc/minis-linux/telegram.conf
```

填写 `MINIS_TELEGRAM_USER_ID`、`MINIS_BASE_URL`（例如 `https://api.example.com/v1`）、`MINIS_MODEL`。Base URL 使用 HTTPS；本机数字回环地址例外。模型需支持 Chat Completions 和工具调用。

用同一个 Bash 终端写入两个密钥；输入不回显，不把值写到命令行或仓库。模型密钥用普通调用 API Key，**不是 CPA 管理密钥**。

```bash
sudo -v
sudo install -m 0600 /dev/null /etc/minis-linux/telegram.token
sudo install -m 0600 /dev/null /etc/minis-linux/model.key
read -rsp 'Telegram Bot Token: ' minis_secret; printf '\n'
printf '%s\n' "$minis_secret" | sudo tee /etc/minis-linux/telegram.token >/dev/null
unset minis_secret
read -rsp '模型 API Key: ' minis_secret; printf '\n'
printf '%s\n' "$minis_secret" | sudo tee /etc/minis-linux/model.key >/dev/null
unset minis_secret
```

## 2. 启动并验收

```bash
sudo install -m 0644 systemd/minis-linux-telegram.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/minis-linux-telegram.service
sudo systemctl daemon-reload
sudo systemctl enable --now minis-linux-telegram.service
sudo systemctl status minis-linux-telegram.service --no-pager
sudo journalctl -u minis-linux-telegram.service -n 30 --no-pager
```

打开专用 bot 私聊：先发 `/status` 验 Telegram 收发，再发一句自然语言验模型。`/status` **不调用模型**。`/new` 清除本机该入口的对话历史（不会删除 Telegram 聊天记录）。不接受群消息或其他账号；配置中的 ID 必须与私聊发送者一致。若报 `409`，检查该新 bot 是否还配置了 webhook 或存在另一个轮询进程；程序不会替你删除 webhook。

## 3. 状态、权限与停止

- `StateDirectory=` 持久化在 `/var/lib/minis-linux-telegram`；使用 `DynamicUser=` 时 systemd 可能把实际目录放在 `/var/lib/private/` 下。会话和轮询游标不与命令行会话共用。日志不打印密钥、消息或模型响应正文。
- Telegram 只能读该目录下 `workspace/` 顶层的普通文本文件，不提供 Shell 或写文件工具。需要处理文件时，只放专用、不含敏感信息的副本；工具结果可能发给模型，也可能进入 Telegram 回复。工作区限制不是通用沙箱；systemd 只允许服务修改自己的状态目录等必要位置。
- 单实例串行处理。新消息保留同一 Telegram 会话上下文，重启后仍保留。超过一小时的缓存消息忽略；为避免重启时重复调用模型，处理前先记住消息序号，进程中途退出或回复发送失败时，该条可能没有回复，需要手动再发。Telegram 网络错误退避重连；无效 token / 轮询冲突停止，先修配置再启动。
- 停用（保留配置和状态）：`sudo systemctl disable --now minis-linux-telegram.service`。修改配置、密钥或应用后用 `sudo systemctl restart minis-linux-telegram.service` 生效。
