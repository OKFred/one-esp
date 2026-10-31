# 温度与环境 MQTT 遥测

固件业务采集和 MQTT 独立于 USB debug 和网页。没有环境配置时，保留既有的 MCU 路径：默认每 60 秒读取原版 ESP32 的 `esp32.raw_temperature()`，把华氏读数换算为 `cpu_temperature_c`；这是芯片内部参考温度。一旦载入或通过配置服务设置环境采集，关闭或读取失败不会自动改成 MCU 数据。启用环境传感器后，同轮真实读取提供 `temperature_c` / `humidity_pct`，并可带上同轮 MCU 内部温度。

## 私有配置

MQTT 和 RS485 现分别提供网页配置入口，底层复用 `mqtt.config.get/set`、`sensor.config.get/set` 服务；原 Wi-Fi / 时区配置仍使用独立的 `.env` 表单。实际 MQTT 配置保存在设备根目录的 `.mqtt.json`，CA 沿用既有 `ca_file` 路径（默认 `.mqtt-ca.der`），RS485 配置保存在 `.sensor.json`。这些文件及其 revision、暂存、恢复、秘密脱敏记录均为私有文件，不进入 Git。模板使用 `apps/firmware/.mqtt.example.json` 和 `.sensor.example.json`；不要把真实账号、密码、完整主题或设备身份写入模板、日志、截图或提交。

配置读取返回 `revision`、公开 `values` 和秘密是否已配置的标志；关闭 USB 调试采集也可回读配置。MQTT 密码不回显，保存时省略或留空均保留原密码。关闭 MQTT 会保留连接参数和密码。修改必须携带读取到的 revision；冲突时重新读取后再保存。MQTT 与 RS485 试用不能同时进行：先返回 `stage=testing`，试用通过后才持久提交并递增 revision，最终返回 `applied`；失败或超时返回 `rolled-back` 并恢复旧配置。不能把初始 `testing` 或 MQTT PUBACK 当成配置已生效。

MQTT 网页可设置开关、主机、TLS 端口、服务器校验名、账号、密码、客户端及信封身份、上报周期、keepalive 和 `commands_enabled`。连接试用最多 45 秒，先关闭旧连接，再用候选配置建立单一 TLS 连接；启用远程命令时，还必须完成命令主题订阅并达到 `ready`。失败恢复原连接和配置。待确认消息保留原始字节、时间、UUID 和 packet ID；未确认时拒绝改变 site / gateway / device / client 身份，防止把旧消息改成新身份。CA 路径和指纹只读，本期没有 CA 上传、任意证书路径或关闭校验选项。

RS485 网页可配置以下字段，采集周期与 MQTT 上报周期分开设置。UART1 引脚固定为 RX14 / TX15、GPIO13 并联 DE 与 /RE，串口保持 8N1；页面修改的是 ESP32 采集参数，不向传感器写地址、波特率或校准寄存器。

| 字段               | 范围或含义                                                       |
| ------------------ | ---------------------------------------------------------------- |
| `enabled`          | 缺失或关闭时不初始化传感器 UART                                  |
| `protocol`         | `ascii_read` 或 `sht20_rtu`，不自动回退协议                      |
| `interval_seconds` | 5–3600 秒；不由 debug 参数或旧 `.env` 的 `POLL_INTERVAL_MS` 控制 |
| `baudrate`         | 9600、14400 或 19200                                             |
| `slave_address`    | RTU 从站地址 1–247；实机地址须读取 `PARAM` 等只读信息确认        |
| `timeout_ms`       | 200–5000ms                                                       |

启用的 RS485 候选配置会进行有界真实读数试用，只有合法响应才提交；失败恢复旧配置。关闭采集可直接保存。协议、地址和串口参数必须与成品模块实际设置一致。`ascii_read` 发送 `READ`，只检查文本完整性、单位和数值范围，没有链路 CRC；`sht20_rtu` 以 FC04 连续读取输入寄存器 `0x0001/0x0002`，温度按有符号 16 位数除以 10，湿度按无符号数除以 10，并强制验证 Modbus CRC16、从站地址、功能码和字节数。CRC 在线路上低字节先发，不能按某些资料中颠倒的表头拼接。CRC 只验证传输完整性，不证明传感器绝对准确度或校准正确。

通过网页或配置命令修改后，由服务完成试用和热应用，无需每次重启或刷机；直接离线修改私有文件不会被持续监视，需重启后加载。上传工具不上传这些私有 JSON 或证书，应单独备份和校验。开启 MQTT 后仍先等待 Wi-Fi 和近期 NTP 同步就绪；临时初始化失败按 30–300 秒退避重试，无效配置保持关闭。根证书须来自已验证的信任链，不应盲目信任网络连接抓到的证书。

设备运行 MicroPython 1.29.0 定制冻结固件，公开应用代码改变后需要重建并更新应用镜像；普通文件系统上传会拒绝冻结 profile，不能用上传成功代替新代码已运行。仓库当前有 23 个运行源码：22 个应用模块加 `boot.py`，包含 `lib/ssd1306.py`。本轮网页配置、RTU 驱动和远程命令代码已完成相应主机测试，但新镜像尚未刷入；USB 配置试用、固件从 ASCII 切到 RTU、远程 MQTT 命令仍待实机验收。

非冻结固件的预编译上传使用 `pnpm device upload --precompile --port COM3`，默认通过隔离的 `uv tool run --from mpy-cross==1.29.0.post2` 调用 MicroPython 1.29.0 编译器，输出 MPY 6.3 纯字节码，不安装全局依赖。旧固件回退时显式添加 `--mpy-version 1.22.2`，使用对应的 MPY 6.2 编译器；该参数不刷写或降级设备上的 MicroPython。

`boot.py` 保留源码入口；其他源码在完整备份、全部暂存哈希和格式校验后移为 `.py.source` 等归档名，以免 `.py` 优先于 `.mpy` 被导入。普通上传会拒绝覆盖已有同名 `.mpy`，防止意外恢复源码加载。全部暂存校验完成后才替换文件；多模块替换不具跨文件事务性，中途断电需从已校验的完整备份恢复。MPY 格式检查包含四字节头和传输哈希，未在设备上预先执行模块。预编译减少源码导入的编译峰值，字节码仍占 RAM，不等于 frozen 固件。

TLS 端口默认 8883，可在 1–65535 范围配置；换端口仍强制使用 TLS、`CERT_REQUIRED`、既有单个 DER CA 和 `server_hostname` 校验，失败时关闭连接并退避，不回退到明文或跳过校验。配置中的 `host` 用于解析；可选数值 IPv4 `broker_ip` 只替代连接地址，不取消服务器主机名校验。常规使用后台 DNS，避免固定地址阻碍服务迁移。现有 CA 保留，固件升级不自动更新信任链。

证书维护须分别处理 broker 叶证书续期、设备信任根、TLS 实现和吊销信息。根证书并非签发日期越新越安全；应核验 CA 官方指纹、有效期、安全公告及实际签发链，再做有备份的受控替换。当前仅限制信任根，并非固定叶证书或 SPKI；不要为修复连接失败导入宽泛 CA 包。`CERT_REQUIRED` 与有效期检查也不等于在线吊销检查：当前应用没有自动 CRL / OCSP 或浏览器紧急撤信列表同步，升级 MicroPython 不会自动补齐这些机制。eMudhra 事件表明中间 CA 的交叉签名路径同样需要审查，不能只按根证书名称判断；参见 [Google 事件公告](https://blog.google/security/protecting-chrome-and-android-users-from-compromised-emudhra-intermediate-ca-certificates/)与 [Sectigo 交叉证书撤销公告](https://www.sectigo.com/resource-library/advisory-on-compromised-emudhra-intermediate-certificates)。

## 信封和投递

发布主题为 `iot/v1/<site_id>/<gateway_id>/devices/<device_id>/telemetry`，与信封身份严格一致。业务遥测使用 MQTT 3.1.1、QoS 1、非 retained；命令订阅另由默认关闭的 `commands_enabled` 控制。示例仅含模拟身份和读数：

```json
{
  "schema_version": 2,
  "message_id": "00000000-0000-4000-8000-000000000001",
  "site_id": "example-site",
  "gateway_id": "example-gateway",
  "device_id": "example-esp32-device",
  "read_time_utc": "2026-10-30T06:00:00Z",
  "sample_time_utc": null,
  "observation_kind": "direct_read",
  "quality": "ok",
  "metrics": { "cpu_temperature_c": 50.0 },
  "source": {
    "driver": "esp32.raw_temperature",
    "sensor_location": "mcu_internal"
  }
}
```

实际消息 ID 使用 `os.urandom()` 生成 UUID v4，时间来自设备真实 UTC RTC。Runtime 要求存在最近 10 分钟内的成功 NTP 同步且 RTC 年份有效；仅有 `synced` 历史标志不够。没有有效时钟或读数时不生成消息、零值或假时间。`read_time_utc` 表示读取时刻；没有独立设备采样时间，因此 `sample_time_utc=null`。

环境模式使用相同信封；以下仅展示模拟读数的 `metrics` 和固定来源标签：

```json
{
  "metrics": {
    "temperature_c": 25.0,
    "humidity_pct": 50.0,
    "cpu_temperature_c": 50.0
  },
  "source": {
    "driver": "rs485.ascii.READ",
    "integrity": "format_only",
    "metric_sources": {
      "temperature_c": "rs485.ascii.READ",
      "humidity_pct": "rs485.ascii.READ",
      "cpu_temperature_c": "esp32.raw_temperature"
    }
  }
}
```

RTU 模式的固定驱动来源改为 `rs485.modbus.sht20`，对应温湿度 `metric_sources` 同样使用该来源，`source.integrity=crc_verified`。`format_only` 不代表 CRC 通过；`quality=ok` 表示该模式的帧、数值与新鲜度检查通过，不是绝对准确度认证。

驱动完整校验一帧后，runtime 在该次读取记录 UTC，并同轮读取可选 MCU 温度；MCU 不可用时省略该指标及其来源。MQTT provider 校验新采样序号、原读取时间、有效范围和固定来源，只复制已知字段；无有效环境观察时不发新消息，不回退成仅 MCU 消息、不补零，也不把旧读数改成新的读取时间。`read_time_utc` 始终是原物理读取 UTC，重试不重新取时。

只保留一条 RAM 待确认消息。重连和未收到 PUBACK 的重试复用原始 ID、时间、payload bytes 和 packet ID，以 DUP 重发。只有匹配的 PUBACK 才确认完成；它仅代表 broker 接收，不代表下游存储成功。待确认消息未结束前不替换该信封。独立环境驱动可以继续周期读取并保留最新观察，但不累计离线分钟记录；重启或断电会丢失 RAM 待发消息，没有持久 outbox 保证。

## MQTT 下发 RS485 配置

设备只订阅自身精确主题，不使用通配符：

- 命令：`iot/v1/<site_id>/<gateway_id>/devices/<device_id>/commands`
- 应用回执：`iot/v1/<site_id>/<gateway_id>/devices/<device_id>/commands/ack`

仅允许 `sensor.config.get` 和 `sensor.config.set`；MQTT 服务器、密码仍通过 USB 网页配置，不开放远程 GPIO、任意寄存器写入或 MQTT 凭据读取。远程业务配置不要求 USB debug 开启。配置 `commands_enabled=true` 只是启用意图，必须看到 `status.mqtt.transport.commands.state=ready` 才表示 SUBACK 完成；`denied` / `unavailable` 不能计为可下发。真实下发验收还须核对请求 ID 对应的最终应用回执。

以下使用示例身份主题 `iot/v1/example-site/example-gateway/devices/example-device/commands`。先读取 revision；时间示例必须替换为发送时 UTC 未来至多 300 秒，不能直接复用过期值：

```json
{
  "v": 1,
  "id": "example-sensor-get-1",
  "command": "sensor.config.get",
  "args": {},
  "expiresAtUtc": "2026-10-31T15:01:00Z"
}
```

再使用设备返回的 revision 修改。地址 1 仅为示例，不代表实机地址：

```json
{
  "v": 1,
  "id": "example-sensor-set-1",
  "command": "sensor.config.set",
  "args": {
    "revision": 0,
    "values": {
      "enabled": true,
      "protocol": "sht20_rtu",
      "slave_address": 1,
      "baudrate": 9600,
      "interval_seconds": 60,
      "timeout_ms": 2000
    }
  },
  "expiresAtUtc": "2026-10-31T15:02:00Z"
}
```

请求必须携带 `v/id/command/args/expiresAtUtc`；ID 为 1–64 个字母、数字、`_` 或 `-`，payload 最多 2048 字节。要求近期同步的 UTC 时钟，拒绝过期、超过 300 秒的远期请求、retained 命令和错误主题；UTC 时间可含小数秒。`set` 需要正确 revision，重复 ID 必须复用相同 payload，不能换参数再用旧 ID。最多缓存 8 条/4096 字节的近期响应；重试不会重复执行，缓存淘汰或重启后的旧修改还受持久 revision 检查约束。

应用回执包含 `v/id/ok` 及 `result` 或 `error`。`get` 返回当前 revision 和公开配置；`set` 可先回 `testing`，最终回 `applied` 或 `rolled-back`，以最终回执的 revision 为准。入站 QoS1 的 PUBACK、业务遥测 PUBACK 和命令应用回执分别处理，不能互相代替；控制帧也不会插入尚未写完的业务 PUBLISH。此代码路径尚未取得新镜像上的远程命令验收，不以主机测试或订阅配置值代替 broker 权限和实机结果。

## 调度和状态

DNS 仅一个后台 worker，缓存 5 分钟，超时不会叠加新线程。主循环的 TCP/TLS/MQTT 使用非阻塞读写，每 tick 最多一次 256 字节写和一次 256 字节读；连接、握手、PUBACK、PING 均有期限，失败按 1–60 秒有界退避。TLS 加密和证书计算仍有 CPU 成本，必须用实机测量 OLED 更新间隔，不能仅凭 mock 测试宣称完全无延迟。

9600 / 8N1 的四字节 `READ` 约需 4.2ms，八字节 RTU 请求约需 8.3ms；发送期间以 1ms 间隔优先轮询 UART 完成并立即切回接收，避免慢 OLED / TLS 工作使快速回复丢头。接收按 `timeout_ms` 限时、最多 128 字节。`status.environment` 保留最后真实值、读取时间、年龄和 stale / 错误状态，并区分协议、`integrity`、CRC 错误数和有界原始帧。网页把过期、失联、读取失败及暂停更新分别提示，不以其他遥测的刷新时间掩盖旧值。

正在实板验证的 TLS 握手优化会暂缓 bulk USB 快照、屏幕编码和资源帧分配；命令 ACK、TTL 到期检查和物理 OLED 继续。它只是在降低握手期间的分配峰值，不能据此宣称长期重连问题的根因已经全部解决。

USB `status.mqtt` 提供采样、ACK、待确认状态和脱敏的连接阶段/失败类别。内部 `lastSample` / `lastAcknowledged` 各只保留最近一条摘要；周期 USB status 仅发送 `lastAcknowledged`，避免重复的大状态帧。摘要：随机消息 UUID、UTC 读取时间、MCU 温度及环境模式的温湿度、原始 payload bytes 的 SHA-256。没有完整消息、账号、地址、主题或 site/gateway/device 身份，不新增历史列表；可用于核对采样与匹配 PUBACK，不能代替 broker 路由或下游存储证据。网页把本地采样和 MQTT 投递分行显示；暂停、失联或旧 status 下显示“上次MQTT状态”，确认数缺失时明确未提供，不补零。关闭 debug 后 MQTT 和已启用的环境读取仍继续；只停止 USB 周期遥测。应用退出会关闭 MQTT 连接、释放传感器 UART 并使 DE 回低，维护用的 `KeyboardInterrupt` 仍可用于 mpremote。

屏幕与网页的“今日已确认”使用 `dailyAcknowledged`，按设备 `TIMEZONE_HOURS` 的本地日期在 0 点清零，以实际处理成功 PUBACK 的日期计数。跨午夜保留待发消息的原始字节、读取时间和身份；确认到达后计入新的一天。断网、关闭 MQTT 或关闭 USB 调试不阻止日期切换。累计 `acknowledged`、采样和错误诊断计数保留本次运行的原含义。每日计数也保存在 RAM，设备重启会从零开始，没有为每条回执写 Flash。

首次校时前、日期不可用或时钟回拨到已统计日期之前时，`dailyAcknowledged=null`，不伪造“今日 0 条”；回到同一日期时保留原计数，前进到下一日期才重新归零。OLED 显示 `MQTT today`，1000 起缩写为 `1K+`，并对百万等边界限制在 16 列；网页保留完整的安全整数。旧固件仅有 `acknowledged` 时，网页明确标记“本次启动已确认”。网页不自行在午夜清空缓存，暂停或断开期间仍标记上次状态，直到收到设备的新数据。

本轮新增的是受限 RS485 配置命令，仍没有 MQTT 远程 debug 命令、Cloudflare 部署或数据库/看板改动。以下历史窗口独立保留：既有 MCU 路径曾完成四轮真实采样 / 匹配 PUBACK 及 OLED / NTP 短时验收，后续长稳发现重连异常。环境传感器有三次临时 `READ` 返回 28.8°C / 48.4%RH，以及后来持续环境采样和六次 PUBACK 的证据。一次服务端踢线后，MicroPython 1.22.2 在 NTP 已同步时仍重连证书验证失败；随后复用 SSLContext 的独立精简测试又遇到 DNS resolve 失败，不能记为重连修复成功。24 小时门槛仍失败，未接入或验收下游 PG / Grafana，详见 [验证记录](validation.md)。

1.29.0 标准固件升级初期仅确认版本、模块接口和私有配置，当时业务 TLS、真实环境 PUBACK、重连及长稳均待验收；后来静态冻结固件和任务 OLED 的短窗、受控本地重连已有单独记录。上述历史结果均不覆盖本轮新增配置 / RTU / 命令订阅代码，新镜像尚未刷入。只读协议探测不等于固件 RTU 模式切换、配置持久化或远程命令验收；代码存在、TLS 成功或单元测试通过也不等于完整投递及长期稳定验收。

传感器启用时先只读入轻量配置，让 Wi-Fi/TLS 优先初始化；取得 MQTT CONNACK 后再导入驱动和初始化 UART，最长等待 20 秒。MQTT 禁用或网络明确失败时，本地采集直接继续。等待期间关闭或维护退出仍可取消初始化。

启用 MQTT 后，runtime 保存原 GC 分配阈值并设为 8192 字节，在退出或初始化失败时恢复。阈值按累计分配量提前触发回收，不是保留 8KB 内存的保证；周期调度没有新增强制 GC。OLED 镜像只缓存成功的原始像素，发送时按 48 字节原始像素生成最多 64 字节 base64 分块，避免完整编码串与大 JSON 同时驻留；完整帧预算仍在写入前检查；短写后的下一帧先补 LF 恢复边界。镜像分配失败不反向判定物理显示失败。

当前 TLS 接口及证书行为参考 [MicroPython 1.29 SSL 文档](https://docs.micropython.org/en/v1.29.0/library/ssl.html)；1.22.2 历史窗口参考 [MicroPython 1.22 SSL 文档](https://docs.micropython.org/en/v1.22.0/library/ssl.html)。
