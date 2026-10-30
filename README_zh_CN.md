# one-esp

ESP32-WROOM-32 / 0.96″ OLED 的 MicroPython 固件与本地 USB 调试工作台。网页查看真实 OLED 帧缓冲、引脚登记状态、运行日志和 Wi-Fi 信息，也能在没有设备时回放演示场景。

本期提供 USB 实机通道、响应式网页和可选 MCU 温度 MQTT 上报；MQTT 远程调试、Cloudflare Workers 和温湿度采集留给后续。回放明确标记模拟数据。传感器已断开，当前启动路径不会初始化 Modbus UART 或驱动传感器引脚。

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

MCU 内部温度的独立 MQTT 上报配置、投递和缓存边界见 [MQTT 遥测说明](docs/mqtt-telemetry.md)。它不依赖网页或 debug 会话，不代表环境温度；实际账号权限和上报成功须分别验收。

1. 接入 ESP32；当前 CH340 / COM3 使用 **115200、8N1、无流控**。不用改设备管理器的 FIFO 设置。
2. 关闭其他串口工具，选择串口连接。Bridge 请求一次快照，持续采集默认关闭。
3. 开启调试。FULL 默认 OLED 上限 2 FPS / GPIO 采样 10 Hz，LITE 默认各 1 Hz。每次会话默认 600 秒，最多 1800 秒；网页默认开启自动续期，在到期前延长正在运行的采集。
4. 「设备已确认」代表固件回执。HTTP `accepted` 只表示转发成功；断线、重启、超时取消待确认操作。

只连接串口会取得一次快照；未开启调试或会话到期时，网页明确显示“采集已暂停”。点击屏幕旁的「开启实时采集」即可恢复，也可点击「获取快照」按需读取。LITE 的帧率和 GPIO 上限均为 1，网页按设备报告恢复参数，发送前再校验。

「自动续期」默认开启，仅对已开启的采集生效，不会自动启动停止中的设备。续期保留档位与频率，不重复发送全量快照。本页成功应用的会话时长会跨同设备、同启动会话的连接重连保留；整页刷新或新开页面使用默认 600 秒续期。手动停止立即生效；关闭网页、断开连接或关闭自动续期后，本页不再续期，没有其他页面继续续期时设备在当前会话到期后暂停。电脑休眠或浏览器冻结时仍可能到期，恢复后重新开启即可。采集设置中显示续期状态和上次成功时间，运行日志记录设备报告的开启、续期与停止。

旧固件未声明 `debug-renew` 能力时，网页会显示不支持自动续期，需要先更新固件；不会用普通开启指令代替安全续期。

网页独立检查 OLED 时钟是否推进，其他遥测或相同画面的重复快照不会掩盖停更；发现异常会显示“画面停更”，并限频请求只读快照。网页通道有独立心跳，静默时自动重连；切回页面时重新检查连接并刷新画面。Bridge 在有效采集期间检测真实设备协议数据是否静默，必要时重连串口。这些恢复操作不会重发配置、输出或重启命令，也不会自动开启已停止的采集。

固件按步骤隔离 USB、网络维护、屏幕与遥测的运行异常，失败步骤退避重试并限频记录日志，其他步骤继续运行。OLED 只有在物理写入成功后才更新镜像，网页不会用电脑时间改写设备画面。断电、拔线、系统休眠或原生驱动永久阻塞仍会中断实时显示，页面会在恢复运行后检查并提示实际状态。

OLED 按 `128×64 MONO_VLSB` 原始 1024 字节绘制 Canvas，顶部黄 / 下方蓝。GPIO 区分输入采样、软件设定、配置记录和未知；未知引脚不会为展示而重配。GPIO36 / SVP、GPIO39 / SVN 是只读输入，浮空读数可能变化。当前只采样这两个输入，电源脚没有电压测量值。

**GPIO 输出白名单为空**，所有输出按钮禁用。OLED 用 GPIO4 / SCL、GPIO5 / SDA；GPIO1 / 3 是 UART0；GPIO6–11 属于 Flash。板图按丝印作参考，不表示所有芯片 GPIO 均从板上引出。

仅保留最新屏幕帧、有界日志队列和引脚变化量；不通过反复中断 REPL 采集。RTC 以整数秒换算时区，屏幕跟随 RTC 秒边界刷新并优先于周期 GPIO。TCP 连通性每分钟测试一次；NTP 启动时同步、60 秒后复测漂移，再按估计误差预算每 30–300 秒校准，目标 500ms。两者均使用非阻塞轮询，debug 开启时继续。校时保留小数秒并补偿网络延迟；网页显示估计误差与校准时间，网络质量差或失联会显示精度待恢复。首次 DNS 解析仍可能暂时延迟 USB 响应，详见 [固件说明](apps/firmware/README.md)。

网页的设备健康卡片通过独立 `metrics` 低频帧显示 CPU 主频、Python 内存、ESP-IDF 堆、Wi-Fi RSSI、文件系统容量和芯片温度。FULL 每 5 秒、LITE 每 15 秒采集常规指标；温度每 30 秒、文件系统每 60 秒刷新，物理 Flash 容量只读取一次。只有开启 debug 或显式请求只读快照时才采集；快照复用缓存，最短 5 秒，慢指标失败也不会被频繁重试。关闭 debug 后停止周期采集，不主动触发 GC、不写文件、不做下载测速；只保留最新指标帧，不塞进每秒 status。旧固件不提供 `metrics` 时仍可使用原有调试功能。

CPU 主频不代表占用率；当前 MicroPython 没有整机 CPU 使用率或 Wi-Fi 流量速率接口，网络卡片以 RSSI 和已有 TCP 延迟辅助判断连接质量。Python 的空闲加已分配内存是动态分配预算，不是物理 RAM；它与 ESP-IDF 堆统计存在重叠，不能相加。文件系统分区容量和物理 Flash 总量分别显示。原版 ESP32 温度接口返回华氏度，转换后的摄氏度仅作芯片内部温度参考，不能代替环境温湿度传感器，见 [官方硬件说明](https://docs.micropython.org/en/v1.22.0/esp32/quickref.html#general-board-control) 与 [内存统计说明](https://docs.micropython.org/en/v1.22.0/library/esp32.html#esp32.idf_heap_info)。

## 配置

设备 `.env` 放在设备根目录，参考 `apps/firmware/.env.example`。真实 `.env`、历史配置和设备备份均被 Git 忽略，不要把密码写进源码。

「设备配置」读取不返回密码，只显示是否已配置。密码留空保持原密码。新 Wi-Fi 试连最多 60 秒，成功才保存并增加 revision；失败回滚并恢复原网络。原子事务用于中途重启恢复，不能替代设备备份。业务配置生效时机见固件说明；sensor 停用时采集间隔只保存。

启动失败 OLED 显示 `Check password`、`AP not found` 或 `Connect timeout`，USB 服务继续运行。认证失败也可能由信号或热点设置引起。

## 备份与上传

主机上的上传工具和固件回归测试统一由 **uv** 管理。`.python-version` 固定 **CPython 3.14.2**，`pyproject.toml` 声明依赖，`uv.lock` 锁定直接与传递依赖；依赖安装在根目录 `.venv/`，无需手动激活环境，也不向全局 Python 安装包。主机 CPython 用于工具和测试，ESP32 仍运行 **MicroPython 1.22.2**。尚未安装 uv 时，按 [官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装。

工具先请求 bridge 释放串口，再备份完整设备到 Git 忽略的 `.device-backups/`，逐文件 SHA-256 校验后上传。备份含私有配置，仅保存在本机。

在仓库根目录执行：

```powershell
uv sync --locked
uv run --locked python tools/device.py backup --port COM3
uv run --locked python tools/device.py upload --precompile --port COM3
```

也可使用 `pnpm device backup --port COM3`、`pnpm device upload --precompile --port COM3`；这两个入口同样使用 `uv run --locked`，按锁文件同步项目环境后调用其中的 Python。

上传中断当前程序；`--precompile` 使用隔离的固定版本 `mpy-cross==1.22.2` 生成纯字节码，`boot.py` 保留源码入口。先完整备份，再暂存全部文件，逐文件核对 SHA-256 和格式 / 编译校验后才替换、归档会遮蔽字节码的旧源码并重启。跨文件替换不具事务性，中途失败需由完整备份恢复。不上传测试和本地 `.env`，保留设备凭据。仅改网页 / bridge 不需上传固件。

## 检查

首次运行前先执行 `uv sync --locked`。`pnpm test:firmware` 通过 `uv run --locked` 使用同一项目环境运行主机回归测试。

```powershell
pnpm typecheck
pnpm lint
pnpm format:check
pnpm test
pnpm test:firmware
pnpm test:tools
pnpm build
pnpm exec playwright install chromium
pnpm test:e2e
```

TypeScript 使用 Go 原生版 TS7，Vue 使用兼容的 TS6 / vue-tsc。Oxlint 做基础检查，ESLint 补 Vue 模板规则；Oxfmt、EditorConfig、Git attributes 统一 **LF（`\n`）**。Iconify 图标离线导入。完整直接依赖见 [依赖清单](docs/dependencies.md)，传输约定见 [协议](docs/protocol.md)，后续规划见 [远程调试设计](docs/remote-debug.md)。
