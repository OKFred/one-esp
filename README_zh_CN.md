# one-esp

ESP32-WROOM-32 / 0.96″ OLED 的 MicroPython 固件与本地 USB 调试工作台。网页查看真实 OLED 帧缓冲、引脚登记状态、运行日志和 Wi-Fi 信息，也能在没有设备时回放演示场景。

本期提供 USB 实机通道与响应式网页；MQTT、Cloudflare Workers 远程调试和温湿度采集留给下一期。回放明确标记模拟数据。传感器已断开，当前启动路径不会初始化 Modbus UART 或驱动传感器引脚。

## 目录

```text
apps/firmware/       MicroPython：运行代码上传到设备根目录
apps/bridge/         Node.js + Hono：串口、命令、回放、HTTP / WebSocket
apps/web/            Vue 3 + Ant Design Vue：桌面 / 平板 / 手机布局
packages/protocol/   协议校验、状态归并、OLED 像素解码
tools/              设备备份与上传工具
e2e/                浏览器端到端检查
docs/               协议、远程设计和验证记录
```

## 启动

使用 `.node-version` 指定的 **Node.js 26.13.0（26 LTS）** 与 **pnpm 11.5.0**。依赖版本固定在清单和 lockfile。

```powershell
fnm install
fnm use
npm install --global pnpm@11.5.0
pnpm install --frozen-lockfile
pnpm dev
```

打开 <http://127.0.0.1:5173>。Bridge 在 `127.0.0.1:8787`，Vite 代理 `/api` 和 `/ws`。点击「场景回放」查看正常启动、Wi-Fi 密码错误、设备离线；回放不控制实物。

生产预览运行 `pnpm build`、`pnpm start`，打开 <http://127.0.0.1:8787>。两种方式只监听本机。网页支持手机尺寸；另一台手机实际访问还需要鉴权和远程入口，本期没有默认向局域网暴露设备控制接口。

## 连接与调试

1. 接入 ESP32；当前 CH340 / COM3 使用 **115200、8N1、无流控**。不用改设备管理器的 FIFO 设置。
2. 关闭其他串口工具，选择串口连接。Bridge 请求一次快照，持续采集默认关闭。
3. 开启调试。FULL 默认 OLED 上限 2 FPS / GPIO 采样 10 Hz，LITE 默认各 1 Hz。默认会话 600 秒，最多 1800 秒，到期关闭。
4. 「设备已确认」代表固件回执。HTTP `accepted` 只表示转发成功；断线、重启、超时取消待确认操作。

OLED 按 `128×64 MONO_VLSB` 原始 1024 字节绘制 Canvas，顶部黄 / 下方蓝。GPIO 区分输入采样、软件设定、配置记录和未知；未知引脚不会为展示而重配。GPIO36 / SVP、GPIO39 / SVN 是只读输入，浮空读数可能变化。当前只采样这两个输入，电源脚没有电压测量值。

**GPIO 输出白名单为空**，所有输出按钮禁用。OLED 用 GPIO4 / SCL、GPIO5 / SDA；GPIO1 / 3 是 UART0；GPIO6–11 属于 Flash。板图按丝印作参考，不表示所有芯片 GPIO 均从板上引出。

仅保留最新屏幕帧、有界日志队列和引脚变化量；不通过反复中断 REPL 采集。RTC 时钟每秒刷新，时区以整数秒换算，屏幕帧优先于周期 GPIO，避免画面被高频引脚变化挤占。TCP 连通性每分钟测试一次，debug 开启时继续；连接使用非阻塞轮询。NTP 每 10 分钟校时，debug 开启时暂停。首次 DNS 解析可能暂时延迟 USB 响应，见 [固件说明](apps/firmware/README.md)。

## 配置

设备 `.env` 放在设备根目录，参考 `apps/firmware/.env.example`。真实 `.env`、历史配置和设备备份均被 Git 忽略，不要把密码写进源码。

「设备配置」读取不返回密码，只显示是否已配置。密码留空保持原密码。新 Wi-Fi 试连最多 60 秒，成功才保存并增加 revision；失败回滚并恢复原网络。原子事务用于中途重启恢复，不能替代设备备份。业务配置生效时机见固件说明；sensor 停用时采集间隔只保存。

启动失败 OLED 显示 `Check password`、`AP not found` 或 `Connect timeout`，USB 服务继续运行。认证失败也可能由信号或热点设置引起。

## 备份与上传

工具先请求 bridge 释放串口，再备份完整设备到 Git 忽略的 `.device-backups/`，逐文件 SHA-256 校验后上传。备份含私有配置，仅保存在本机。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r tools/requirements.txt
.\.venv\Scripts\python.exe tools/device.py backup --port COM3
.\.venv\Scripts\python.exe tools/device.py upload --port COM3
```

上传中断当前程序，先上传临时 `.py` 文件并编译校验，然后替换并重启。不上传测试和本地 `.env`，保留设备凭据。仅改网页 / bridge 不需上传固件。`pnpm device` 使用 PATH 上的 Python，推荐直接用虚拟环境命令。

## 检查

```powershell
pnpm typecheck
pnpm lint
pnpm format:check
pnpm test
pnpm test:firmware
pnpm build
pnpm exec playwright install chromium
pnpm test:e2e
```

TypeScript 使用 Go 原生版 TS7，Vue 使用兼容的 TS6 / vue-tsc。Oxlint 做基础检查，ESLint 补 Vue 模板规则；Oxfmt、EditorConfig、Git attributes 统一 **LF（`\n`）**。Iconify 图标离线导入。完整直接依赖见 [依赖清单](docs/dependencies.md)，传输约定见 [协议](docs/protocol.md)，后续规划见 [远程调试设计](docs/remote-debug.md)。
