# MCU 温度 MQTT 遥测

固件可以独立于 USB debug 和网页，每 60 秒直接读取原版 ESP32 的 `esp32.raw_temperature()`，把华氏读数换算为摄氏值，以 `cpu_temperature_c` 上报。这是芯片内部参考温度，不是环境温度或温湿度传感器读数；断开的传感器仍不初始化。

## 私有配置

以 `apps/firmware/.mqtt.example.json` 为字段模板，将实际配置单独保存到设备根目录的 `.mqtt.json`，CA 保存为 `.mqtt-ca.der`。两类实际文件均被 Git 忽略；不要把真实账号、密码、完整主题或设备身份写入模板、日志、截图或提交。配置不通过现有 Wi-Fi 配置表单读写。

缺少配置或 `enabled=false` 时不加载 MQTT/TLS 模块、不启动连接。开启后也先等待 Wi-Fi 和近期 NTP 同步就绪，再加载 MQTT、CA 和客户端；临时初始化失败按 30–300 秒退避重试，无效配置保持关闭直到重启。修改私有配置后重启应用生效。上传工具不上传这些私有 JSON 或证书，需要单独备份、校验和部署。根证书必须来自已验证的信任链，不应盲目信任网络连接抓到的证书。

当前实板使用与 MicroPython 1.22.2 匹配的纯字节码 `.mpy`，避免源码导入的编译峰值挤占 Wi-Fi/TLS 原生堆：`pnpm device upload --precompile --port COM3`。编译器由隔离的 `uv tool run --from mpy-cross==1.22.2` 固定版本，不安装全局依赖。`boot.py` 保留源码入口；其他源码在完整备份、全部暂存哈希和格式校验后移为 `.py.source` 等归档名，以免 `.py` 优先于 `.mpy` 被导入。普通上传会拒绝覆盖已有同名 `.mpy`，防止意外恢复源码加载。全部暂存校验完成后才替换文件；多模块替换不具跨文件事务性，中途断电需从已校验的完整备份恢复。MPY 格式检查包含四字节头和传输哈希，未在设备上预先执行模块。预编译减少编译峰值，字节码仍占 RAM，不等于 frozen 固件。

TLS 固定使用 8883、`CERT_REQUIRED`、单个 DER CA 和 `server_hostname` 校验，失败时关闭连接并退避，不回退到明文或跳过校验。配置中的 `host` 用于解析；可选数值 IPv4 `broker_ip` 只替代连接地址，不取消服务器主机名校验。常规使用后台 DNS，避免固定地址阻碍服务迁移。

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

只保留一条 RAM 待确认消息。重连和未收到 PUBACK 的重试复用原始 ID、时间、payload bytes 和 packet ID，以 DUP 重发。只有匹配的 PUBACK 才确认完成；它仅代表 broker 接收，不代表下游存储成功。离线期间不累计每分钟记录，待确认消息未结束前不采下一条；重启或断电会丢失 RAM 待发消息，没有持久 outbox 保证。

## 调度和状态

DNS 仅一个后台 worker，缓存 5 分钟，超时不会叠加新线程。主循环的 TCP/TLS/MQTT 使用非阻塞读写，每 tick 最多一次 256 字节写和一次 256 字节读；连接、握手、PUBACK、PING 均有期限，失败按 1–60 秒有界退避。TLS 加密和证书计算仍有 CPU 成本，必须用实机测量 OLED 更新间隔，不能仅凭 mock 测试宣称完全无延迟。

USB `status.mqtt` 提供采样、ACK、待确认状态和脱敏的连接阶段/失败类别。`lastSample` / `lastAcknowledged` 各只保留最近一条摘要：随机消息 UUID、UTC 读取时间、真实摄氏值、原始 payload bytes 的 SHA-256。没有完整消息、账号、地址、主题或 site/gateway/device 身份，不新增历史列表；可用于核对采样与匹配 PUBACK，不能代替 broker 路由或下游存储证据。关闭 debug 后 MQTT 仍按业务周期运行；只停止 USB 遥测，不停止温度上报。应用退出会关闭 MQTT 连接，维护用的 `KeyboardInterrupt` 仍可用于 mpremote。

这期没有 MQTT 远程 debug 命令、Cloudflare 部署或数据库/看板改动。当前环境已完成四轮真实采样 / 匹配 PUBACK 及 OLED / NTP 短时验收，详见 [验证记录](validation.md)。其他环境仍需分别确认账号权限和实际投递；代码存在、TLS 成功或单元测试通过均不等于已成功上报。

启用 MQTT 后，runtime 保存原 GC 分配阈值并设为 8192 字节，在退出或初始化失败时恢复。阈值按累计分配量提前触发回收，不是保留 8KB 内存的保证；周期调度没有新增强制 GC。OLED 镜像只缓存成功的原始像素，发送时才生成 base64，串口按前缀 / JSON / LF 分段写入；短写后的下一帧先补 LF 恢复边界。镜像分配失败不反向判定物理显示失败。

TLS 接口及证书行为参考 [MicroPython 1.22 SSL 文档](https://docs.micropython.org/en/v1.22.0/library/ssl.html)。
