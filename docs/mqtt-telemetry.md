# 温度与环境 MQTT 遥测

固件业务采集和 MQTT 独立于 USB debug 和网页。未启用环境传感器时，默认每 60 秒读取原版 ESP32 的 `esp32.raw_temperature()`，把华氏读数换算为 `cpu_temperature_c`；这是芯片内部参考温度。启用环境传感器后，同轮真实读取提供 `temperature_c` / `humidity_pct`，并可带上同轮 MCU 内部温度。

## 私有配置

以 `apps/firmware/.mqtt.example.json` 为字段模板，将实际配置单独保存到设备根目录的 `.mqtt.json`，CA 保存为 `.mqtt-ca.der`。两类实际文件均被 Git 忽略；不要把真实账号、密码、完整主题或设备身份写入模板、日志、截图或提交。配置不通过现有 Wi-Fi 配置表单读写。

环境读取另用设备根目录的私有 `.sensor.json`，参考默认禁用的 `apps/firmware/.sensor.example.json`。缺失或 `enabled=false` 时不初始化传感器 UART；设为 `{"enabled":true,"interval_seconds":60}` 并重启应用后，固定驱动使用 UART1 RX14 / TX15、GPIO13 并联 DE 与 /RE，只发送 `READ`。环境采样的开关和周期不由网页 debug 参数或旧 `.env` 的 `POLL_INTERVAL_MS` 控制。实际 `.sensor.json` 同样不进 Git，须单独备份和部署。

缺少配置或 `enabled=false` 时不加载 MQTT/TLS 模块、不启动连接。开启后也先等待 Wi-Fi 和近期 NTP 同步就绪，再加载 MQTT、CA 和客户端；临时初始化失败按 30–300 秒退避重试，无效配置保持关闭直到重启。修改私有配置后重启应用生效。上传工具不上传这些私有 JSON 或证书，需要单独备份、校验和部署。根证书必须来自已验证的信任链，不应盲目信任网络连接抓到的证书。

当前实板使用与 MicroPython 1.22.2 匹配的纯字节码 `.mpy`，避免源码导入的编译峰值挤占 Wi-Fi/TLS 原生堆：`pnpm device upload --precompile --port COM3`。编译器由隔离的 `uv tool run --from mpy-cross==1.22.2` 固定版本，不安装全局依赖。`boot.py` 保留源码入口；其他源码在完整备份、全部暂存哈希和格式校验后移为 `.py.source` 等归档名，以免 `.py` 优先于 `.mpy` 被导入。普通上传会拒绝覆盖已有同名 `.mpy`，防止意外恢复源码加载。全部暂存校验完成后才替换文件；多模块替换不具跨文件事务性，中途断电需从已校验的完整备份恢复。MPY 格式检查包含四字节头和传输哈希，未在设备上预先执行模块。预编译减少编译峰值，字节码仍占 RAM，不等于 frozen 固件。

TLS 固定使用 8883、`CERT_REQUIRED`、单个 DER CA 和 `server_hostname` 校验，失败时关闭连接并退避，不回退到明文或跳过校验。配置中的 `host` 用于解析；可选数值 IPv4 `broker_ip` 只替代连接地址，不取消服务器主机名校验。常规使用后台 DNS，避免固定地址阻碍服务迁移。现有 CA 保留，固件升级不自动更新信任链。

## 信封和投递

发布主题为 `iot/v1/<site_id>/<gateway_id>/devices/<device_id>/telemetry`，与信封身份严格一致。MQTT 3.1.1、QoS 1、非 retained、只发布，不订阅命令。示例仅含模拟身份和读数：

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
    "metric_sources": {
      "temperature_c": "rs485.ascii.READ",
      "humidity_pct": "rs485.ascii.READ",
      "cpu_temperature_c": "esp32.raw_temperature"
    }
  }
}
```

驱动完整校验一帧后，runtime 在该次读取记录 UTC，并同轮读取可选 MCU 温度；MCU 不可用时省略该指标及其来源。MQTT provider 校验新采样序号、原读取时间、有效范围和固定来源，只复制已知字段；无有效环境观察时不发新消息，不回退成仅 MCU 消息、不补零，也不把旧读数改成新的读取时间。`read_time_utc` 始终是原物理读取 UTC，重试不重新取时。

只保留一条 RAM 待确认消息。重连和未收到 PUBACK 的重试复用原始 ID、时间、payload bytes 和 packet ID，以 DUP 重发。只有匹配的 PUBACK 才确认完成；它仅代表 broker 接收，不代表下游存储成功。待确认消息未结束前不替换该信封。独立环境驱动可以继续周期读取并保留最新观察，但不累计离线分钟记录；重启或断电会丢失 RAM 待发消息，没有持久 outbox 保证。

## 调度和状态

DNS 仅一个后台 worker，缓存 5 分钟，超时不会叠加新线程。主循环的 TCP/TLS/MQTT 使用非阻塞读写，每 tick 最多一次 256 字节写和一次 256 字节读；连接、握手、PUBACK、PING 均有期限，失败按 1–60 秒有界退避。TLS 加密和证书计算仍有 CPU 成本，必须用实机测量 OLED 更新间隔，不能仅凭 mock 测试宣称完全无延迟。

9600 / 8N1 的四字节 `READ` 约需 4.2ms；发送期间以 1ms 间隔优先轮询 UART 完成并立即切回接收，避免慢 OLED / TLS 工作使快速回复丢头。接收限时 2 秒、最多 128 字节。`status.environment` 保留最后真实值、读取时间、年龄和 stale / 错误状态；网页把过期、失联、读取失败及暂停更新分别提示，不以其他遥测的刷新时间掩盖旧值。

正在实板验证的 TLS 握手优化会暂缓 bulk USB 快照、屏幕编码和资源帧分配；命令 ACK、TTL 到期检查和物理 OLED 继续。它只是在降低握手期间的分配峰值，不能据此宣称长期重连问题的根因已经全部解决。

USB `status.mqtt` 提供采样、ACK、待确认状态和脱敏的连接阶段/失败类别。内部 `lastSample` / `lastAcknowledged` 各只保留最近一条摘要；周期 USB status 仅发送 `lastAcknowledged`，避免重复的大状态帧。摘要：随机消息 UUID、UTC 读取时间、MCU 温度及环境模式的温湿度、原始 payload bytes 的 SHA-256。没有完整消息、账号、地址、主题或 site/gateway/device 身份，不新增历史列表；可用于核对采样与匹配 PUBACK，不能代替 broker 路由或下游存储证据。网页把本地采样和 MQTT 投递分行显示；暂停、失联或旧 status 下显示“上次MQTT状态”，确认数缺失时明确未提供，不补零。关闭 debug 后 MQTT 和已启用的环境读取仍继续；只停止 USB 周期遥测。应用退出会关闭 MQTT 连接、释放传感器 UART 并使 DE 回低，维护用的 `KeyboardInterrupt` 仍可用于 mpremote。

这期没有 MQTT 远程 debug 命令、Cloudflare 部署或数据库/看板改动。既有 MCU 路径曾完成四轮真实采样 / 匹配 PUBACK 及 OLED / NTP 短时验收，后续长稳发现重连异常。环境传感器有三次临时 `READ` 返回 28.8°C / 48.4%RH，以及后来持续环境采样和六次 PUBACK 的证据。一次服务端踢线后，MicroPython 1.22.2 在 NTP 已同步时仍重连证书验证失败；随后复用 SSLContext 的独立精简测试又遇到 DNS resolve 失败，不能记为重连修复成功。24 小时门槛仍失败，未接入或验收下游 PG / Grafana，详见 [验证记录](validation.md)。

计划从 1.22.2 升级到 1.29.0 做对照，但尚未刷写或取得新版本实机结果。旧版本历史窗口和主机测试不能覆盖这项验收；需在匹配字节码、保留 CA 与配置备份的前提下重新验证故障路径。代码存在、TLS 成功或单元测试通过均不等于完整投递验收。

传感器启用时先只读入轻量配置，让 Wi-Fi/TLS 优先初始化；取得 MQTT CONNACK 后再导入驱动和初始化 UART，最长等待 20 秒。MQTT 禁用或网络明确失败时，本地采集直接继续。等待期间关闭或维护退出仍可取消初始化。

启用 MQTT 后，runtime 保存原 GC 分配阈值并设为 8192 字节，在退出或初始化失败时恢复。阈值按累计分配量提前触发回收，不是保留 8KB 内存的保证；周期调度没有新增强制 GC。OLED 镜像只缓存成功的原始像素，发送时按 48 字节原始像素生成最多 64 字节 base64 分块，避免完整编码串与大 JSON 同时驻留；完整帧预算仍在写入前检查；短写后的下一帧先补 LF 恢复边界。镜像分配失败不反向判定物理显示失败。

TLS 接口及证书行为参考 [MicroPython 1.22 SSL 文档](https://docs.micropython.org/en/v1.22.0/library/ssl.html)。
