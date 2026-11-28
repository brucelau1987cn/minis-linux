# Telegram 常驻部署（systemd）

需要 Linux、systemd 247+、Python 3.10+、系统 CA 证书，以及到 Telegram Bot API 和模型网关的出站连接。**不用开端口，不要复用 Hermes 或其他程序正在使用的 Bot Token。** 先在 @BotFather 创建专用 bot，关闭加入群组的权限；拿到自己的 Telegram 数字用户 ID（不是 @用户名）。

## 一键部署（推荐）

```bash
git clone https://github.com/brucelau1987cn/minis-linux.git && cd minis-linux && sudo /usr/bin/python3 -EsB install.py
```

需要 Python 3.10+（`/usr/bin/python3`）、Git、sudo 和连接到服务器的交互终端。只从信任的仓库或版本执行安装器。首次安装时，输入 Telegram 数字用户 ID、模型 Base URL 和普通调用 API Key，选择网关通过 `/models` 返回的模型，再填写专用 Bot Token。输入密钥时只显示 `*`，不会显示原文。选模不调用推理；网关目录不可用时可手填模型 ID，取消选模不会写入安装配置。已有完整配置时不重新选模、不问密钥，更新代码后重启并设置开机启动。现有配置不完整时停止，不覆盖。安装器不替你安装系统依赖、不创建 Telegram Bot、不开放防火墙，也不会自动更新代码。用 `python3 install.py --check` 可只校验安装包和 unit，不安装、不联网。

**终端有录制时，不要在终端输入密钥。** `sudo`/堡垒机的输入日志可记录不回显的内容。通过受信任的安全文件传输或密钥管理流程，将 `telegram.token` 与 `model.key` 预先放在仓库外本人持有的私有目录中（目录 `0700`，文件 `0600`，每个文件一行）。然后执行 `sudo /usr/bin/python3 -EsB install.py --credentials-dir "$HOME/.config/minis-install-input"`，向导只问非密钥配置，安装器从文件读取密钥。按本机凭据管理约定处理输入文件，不要提交进 Git。

systemd 在程序校验本机配置和初始化状态后才标记启动成功。若更新或启动失败，安装器尝试恢复原程序和服务文件，以及更新前运行中的服务；不会回退聊天或删除凭据。Telegram、模型网关连通性仍需用 `/status` 和普通对话分别验证。

下列是可选的手工步骤；如果已用一键部署，跳过第 1 节和第 2 节的安装命令，直接去 Bot 私聊验收。

## 1. 手工安装应用和配置

在本仓库目录运行。脚本和服务文件由 root 安装；服务进程使用 systemd 分配的非 root 动态用户。下方 `install ... telegram.conf` 只在首次安装时运行，升级时保留现有配置和密钥。

```bash
sudo install -d -m 0755 /opt/minis-linux
sudo install -m 0644 minis.py minis_*.py VERSION /opt/minis-linux/
sudo install -d -m 0755 /opt/minis-linux/skills/weekly-report
sudo install -m 0644 skills/weekly-report/SKILL.md /opt/minis-linux/skills/weekly-report/
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

打开专用 bot 私聊：先发 `/status` 验 Telegram 收发，再发一句自然语言验模型。`/status` **不调用模型**。不接受群消息或其他账号；配置中的 ID 必须与私聊发送者一致。若报 `409`，检查该新 bot 是否还配置了 webhook 或存在另一个轮询进程；程序不会替你删除 webhook。

## 3. 状态、权限与停止

- `StateDirectory=` 持久化在 `/var/lib/minis-linux-telegram`；使用 `DynamicUser=` 时 systemd 可能把实际目录放在 `/var/lib/private/` 下。会话和轮询游标不与命令行会话共用。日志不打印密钥、消息或模型响应正文。
- Telegram 提供 `/time` 和 `/disk` 查询服务器时间和工作区所在文件系统的容量，不调用模型。模型可以读取 `workspace/` 顶层的普通文本文件，不能自行写入；创建、编辑、回收和恢复须经确认。不提供 Shell。需要处理文件时，只放专用、不含敏感信息的副本；工具结果可能发给模型，也可能进入 Telegram 回复。工作区限制不是通用沙箱；systemd 只允许服务修改自己的状态目录等必要位置。
- Telegram 收消息与模型请求分开处理。一次只执行一个模型任务；忙时收到的新普通消息会明确提示**未排队**，不自动重试。`/status` 查询阶段和上次任务结果，`/stop` 停止尚未提交的任务。已停止的任务不会再保存为会话或发送结果；旧请求可能仍在模型网关执行，退出前占用唯一任务位。已经提交、正在发送的回复不能撤回。进行中要清空会话，请先 `/stop` 再 `/new`。
- 已完成的会话重启后仍保留，进行中的任务不会自动重跑。超过一小时的缓存消息忽略；处理前先记住消息序号，进程中途退出或回复发送失败时，该条可能没有回复。Telegram **收消息**失败会退避重连；**发送回复**被限流时，仍处理收到的 `/stop`，只是确认消息可能发不出去。限流结束后可用 `/status` 确认结果。无效 token 或轮询冲突时服务停止，先修配置再启动。
- 停用（保留配置和状态）：`sudo systemctl disable --now minis-linux-telegram.service`。修改配置、密钥或应用后用 `sudo systemctl restart minis-linux-telegram.service` 生效。

## 4. 技能与长期记忆

- 普通消息、技能调用和记忆管理都在**同一个私聊**。发 `/skills` 查看技能，再发「用 weekly-report 技能整理周报」。技能放在 `/opt/minis-linux/skills/<名称>/SKILL.md`；普通用户前台运行默认读 `<状态目录>/skills`，也可用 `--skills-dir` 指定。仅安装信任的本地文本；格式参考仓库示例，要求单行 `description:`。模型先看到目录，按需用 `skill_view` 读取正文。不执行脚本，不安装、更新或操作其他系统的 Skills。
- 发「记住：周报用中文」显式保存，发「查看记忆」查看条目，「忘记第 1 条」删除对应编号。编号固定，删除后不重用。最多 32 条，每条不超过 240 字符（单行）。模型只可提议，未经确认不会写入。记忆在本机以明文保存，后续模型请求会携带当前条目；不要存密钥或隐私资料。若长期记忆文件或技能目录不可读，普通聊天仍可用，回复会提示暂时不用对应功能。
- v0.4 的 `/new` 开启新会话，保留旧历史。忘记某条不会抹去之前普通对话、远端模型记录或 Telegram 聊天记录。技能正文可能存在旧聊天上下文；如果撤下技能并希望旧内容不再随对话发送，开启新会话。

## 5. 定时任务、附件和备用模型（v0.4）

- 发「10分钟后提醒我喝水」，按 Bot 给出的完整命令确认。发 `/schedule help` 查看周期、只读模型任务和管理命令。默认 UTC；可在 `/etc/minis-linux/telegram.conf` 设置 `MINIS_SCHEDULE_TZ=Asia/Shanghai`。服务重启后恢复计划；最多补最近一次符合条件的过期周期，超过 24 小时不补。已领取的运行不重放，故障时可能漏发，不用于必须送达的告警。
- 支持 UTF-8 文本（32 KiB）和有文本层的 PDF（5 MiB）。PDF 需要 `/usr/bin/pdftotext`（Debian/Ubuntu 的 `poppler-utils`）；不支持扫描件 OCR 或加密 PDF。文本和提取的内容发给配置的模型做单次分析，不写入会话。`/send 文件名` 对工作区顶层的支持格式生成预览，确认后发回本私聊；原附件不脱敏。
- 图片分析默认关闭。需要系统 Python 的 Pillow（如 `python3-pil`）和支持图片的模型。设 `MINIS_VISION=1` 后，符合限制的 JPG、PNG 或 WebP 图片（最多 1 MiB、1600 万像素）会发送给配置的模型服务。
- 语音转写默认关闭。设置 `MINIS_VOICE_MODEL` 为可信的本地 Whisper 模型绝对路径，并准备 `/usr/bin/ffmpeg` 与 `/usr/local/bin/whisper-cli`。模型文件须能被服务读取，不应放在受 `ProtectHome` 限制的家目录。音频通过 Telegram 接收并在本机转写，不发送给模型网关；最多 2 MiB、120 秒。
- `MINIS_FALLBACK_MODEL` 可选，仅指定当前 Telegram 网关内的模型 ID。主模型遇到临时故障，且尚未输出或调用工具，才会尝试一次。它不改默认值，不读取终端的备用模型策略；更换服务网关时，请同时复核或清空备用模型配置。

改动这些设置后先用 `--check-config` 配合服务同等凭据环境校验，或由安装器启动检查；改动不会自动重载。

## 6. 更新与恢复

已用新版安装器安装后，在任意目录执行 `minis update --check` 查看候选版本，执行 `minis update` 更新。更新需要 Git 2.34+、支持 SSH 签名的 OpenSSH（`/usr/bin/ssh-keygen`）、sudo 和到 GitHub 的 HTTPS 连接。只有通过内置公钥验签的 `vX.Y.Z` 标签才可安装。若旧版还没有 `minis` 命令，在新版源码目录运行安装器；已有完整配置时不会重新询问凭据。

若安装被强制中断，下次安装会先尝试恢复上一版。也可从可信安装包执行 `sudo /usr/bin/python3 -EsB install.py --recover`，只恢复中断前的程序、版本和服务。版本号只在服务通过本机启动检查后写入。

`minis` 打开本地终端对话。默认数据在 `~/.local/share/minis-linux-terminal`，不读取 systemd 服务的密钥或 Telegram 聊天记录；不能放到 Git 工作树内。首次交互输入后保存的 API Key 为本机 `0600` 明文；通过 `MINIS_API_KEY` 提供的密钥不落盘。`minis --demo` 用独立临时状态，不改真实会话。终端和 Telegram 都不开放 Shell。
