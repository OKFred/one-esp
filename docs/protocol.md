# USB 协议 v1

一行 UTF-8 JSON，前缀 `@one-esp `，结尾 LF。普通输出作为日志。最大行长 8192 字节，超长丢弃到下一个 LF 后恢复。完整运行时校验和类型以 `packages/protocol/src/index.ts` 为准。

Bridge 严格解码 UTF-8；任一坏字节或行尾不完整字符会使整行被丢弃，不以 U+FFFD 替换后继续当成合法 JSON。`serialDiagnostics` 在 `/api/state` 和 bridge 快照中提供进程累计的 `invalidUtf8Lines`、`invalidProtocolLines`、`oversizedLines`；日志滚动和设备重连不会清空计数。警告不回传坏行原文。

环境状态声明 `sht20_rtu` / `crc_verified` 时，主机还独立核对 9 字节 FC04 原始回复的地址范围、CRC16 与温湿度数值；仅允许 `1e-4` 的浮点序列化误差，不能用传感器标称精度放宽传输校验。无 CRC 的 ASCII、旧固件及失败时保留旧值的状态保持兼容。此检查不等于整个 USB 消息已有密码学认证或校验和；合法 ASCII 损坏仍有检测边界。

```json
{
  "v": 1,
  "deviceId": "unique-id",
  "bootId": "random-per-boot",
  "seq": 1,
  "ts": 1200,
  "type": "status",
  "data": {}
}
```

`ts` 是本次启动毫秒数，不是 Unix 时间。`seq` 是固件本次启动序号；bridge 日志另有序号，不可据此跨来源排序去重。bootId 改变清空旧快照和待确认命令。

| 事件    | data                                                                                                         |
| ------- | ------------------------------------------------------------------------------------------------------------ |
| hello   | firmware、board、capabilities                                                                                |
| status  | uptimeMs、memFree、wifi、sensorEnabled；维护状态扩展                                                         |
| metrics | sampledAtMs、intervalMs、cpuHz、memory、idfHeap、storage、flashBytes、temperature、rssiDbm、sampleDurationUs |
| debug   | enabled、profile、expiresInMs、oledFps、gpioHz、droppedLogs                                                  |
| oled    | width=128、height=64、format=MONO_VLSB、buffer（1024 字节 base64）、available、text                          |
| pins    | pins 数组；partial=true 按 gpio 合并变化，false 替换全量                                                     |
| log     | level、message；不得含凭据                                                                                   |
| ack     | id、ok、result 或 error                                                                                      |

`metrics` 是可选能力，由 hello.capabilities 中的 `metrics` 标记；旧固件没有该事件时无需阻断其他功能。所有采样时间均为本次启动毫秒数。字段结构如下，无法读取的指标为 null：

| 字段                    | 单位 / 语义                                                                    |
| ----------------------- | ------------------------------------------------------------------------------ |
| sampledAtMs、intervalMs | 常规指标的采样时刻、发布周期；FULL 5000ms / LITE 15000ms                       |
| cpuHz                   | CPU 主频 Hz，不是占用率                                                        |
| memory                  | freeBytes、allocatedBytes、totalBytes；Python 动态分配预算，不是物理 RAM       |
| idfHeap                 | totalBytes、freeBytes、largestBlockBytes；ESP-IDF 数据堆，与 Python 内存有重叠 |
| storage                 | sampledAtMs、totalBytes、freeBytes；根文件系统分区，60 秒刷新                  |
| flashBytes              | 物理 Flash 字节数，只查询一次，与文件系统容量分开                              |
| temperature             | sampledAtMs、celsius；30 秒刷新，原版 ESP32 华氏度转摄氏度的芯片内部参考温度   |
| rssiDbm                 | Wi-Fi 信号 dBm，未连接时为 null，不代表网络吞吐量                              |
| sampleDurationUs        | 本次采集耗时，微秒；不含 USB 发送时间                                          |

只有有效 debug 会话或显式只读 snapshot 请求触发采集。快照保留最短 5 秒缓存，温度与文件系统读取失败也分别受 30/60 秒间隔限制；debug 关闭后没有后台周期采集。指标独立成帧（约 1KB 以内），只保留最新值，沿用发送预算与每 tick 最多一帧的限制；不附加到每秒 status。采样不主动 GC、不写文件、不下载测速。当前官方 API 不提供 CPU 占用率或 WLAN 流量速率；内存不同口径不能相加，内部温度不能充当环境温度。

```json
{
  "v": 1,
  "id": "unique-command-id",
  "bootId": "current-boot",
  "command": "debug.set",
  "args": { "enabled": true, "profile": "full", "ttlSeconds": 600 }
}
```

| 命令           | 参数 / 约束                                                                    |
| -------------- | ------------------------------------------------------------------------------ |
| debug.set      | enabled、可选 profile / ttlSeconds / oledFps / gpioHz                          |
| debug.renew    | ttlSeconds（1–1800 秒）；当前 bootId、已开启且未到期的会话，保持现有档位和频率 |
| snapshot.get   | hello/status/debug/可选 metrics/全量 pins/最新 oled，debug 关闭也可用          |
| config.get     | revision、公开 values、secrets 布尔配置状态、生效时机；无密码                  |
| config.set     | revision、values；当前 bootId 和有效 debug 会话                                |
| device.restart | 当前 bootId 和有效 debug 会话；ACK 后重启                                      |
| gpio.write     | gpio/value，另需设备输出白名单，目前为空                                       |

POST `/api/command` 返回 `{id,accepted:true}` 后，通过 `/ws` 等待同 id ACK。Wi-Fi 修改先回 `stage=testing`，需继续等 `applied` 或 `rolled-back`。重启 ACK 表示准备重启，之后新 bootId 才说明重新上线。超时后不要自动重发写操作。

支持安全续期的固件在 hello.capabilities 中声明 `debug-renew`。`debug.renew` 只延长当前有效会话，不能开启已停止或到期的采集；手动关闭后延迟到达的续期会被拒绝，重启后的旧 bootId 也会被拒绝。成功 ACK 返回实际 debug 状态，并立即更新 debug 事件；续期不触发全量快照，不改变现有档位或采样频率。旧固件没有该能力时不能退回使用 `debug.set enabled=true` 自动重开。Bridge 根据设备报告记录开启、续期与停止，不推测停止原因。

设备缓存最近 16 条回执去重，不构成跨重启持久化幂等。USB 使用 UART0/stdin/stdout，禁止同时用 mpremote / Thonny 占用，上传前应释放 bridge 连接。

WebSocket 首包为 `snapshot`（BridgeState），随后为 `event`（DeviceEvent）或 `connection`。归并器保留最新 OLED / pins / status / metrics 和最多 200 条日志。慢客户端超过缓冲上限会断开，重连获取新快照。

WebSocket 另每 5 秒发送 `{"type":"heartbeat","data":{"ts":1793330000000}}`（ts 为 bridge 的 Unix 毫秒），用于网页通道活性检测，不代表设备有新数据，也不更新设备 `lastSeen`。网页通道持续静默时重连；服务端通过原生 ping/pong 清理无响应客户端。OLED 时钟进度与其他遥测独立检查，恢复时仅限频请求 `snapshot.get`；不会重发写操作或以本机时间生成设备画面。

固件 GPIO 快照按 6 项分片，每片均为 `partial=true`。主机在 bootId / 会话变化时清空，再按 GPIO 合并；同次启动刷新时保留此前已知记录，避免坏片使完整引脚表被首片清空。
