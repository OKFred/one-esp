# USB 协议 v1

一行 UTF-8 JSON，前缀 `@one-esp `，结尾 LF。普通输出作为日志。最大行长 8192 字节，超长丢弃到下一个 LF 后恢复。完整运行时校验和类型以 `packages/protocol/src/index.ts` 为准。

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

| 事件   | data                                                                                |
| ------ | ----------------------------------------------------------------------------------- |
| hello  | firmware、board、capabilities                                                       |
| status | uptimeMs、memFree、wifi、sensorEnabled；维护状态扩展                                |
| debug  | enabled、profile、expiresInMs、oledFps、gpioHz、droppedLogs                         |
| oled   | width=128、height=64、format=MONO_VLSB、buffer（1024 字节 base64）、available、text |
| pins   | pins 数组；partial=true 按 gpio 合并变化，false 替换全量                            |
| log    | level、message；不得含凭据                                                          |
| ack    | id、ok、result 或 error                                                             |

```json
{
  "v": 1,
  "id": "unique-command-id",
  "bootId": "current-boot",
  "command": "debug.set",
  "args": { "enabled": true, "profile": "full", "ttlSeconds": 600 }
}
```

| 命令           | 参数 / 约束                                                   |
| -------------- | ------------------------------------------------------------- |
| debug.set      | enabled、可选 profile / ttlSeconds / oledFps / gpioHz         |
| snapshot.get   | hello/status/debug/全量 pins/最新 oled，debug 关闭也可用      |
| config.get     | revision、公开 values、secrets 布尔配置状态、生效时机；无密码 |
| config.set     | revision、values；当前 bootId 和有效 debug 会话               |
| device.restart | 当前 bootId 和有效 debug 会话；ACK 后重启                     |
| gpio.write     | gpio/value，另需设备输出白名单，目前为空                      |

POST `/api/command` 返回 `{id,accepted:true}` 后，通过 `/ws` 等待同 id ACK。Wi-Fi 修改先回 `stage=testing`，需继续等 `applied` 或 `rolled-back`。重启 ACK 表示准备重启，之后新 bootId 才说明重新上线。超时后不要自动重发写操作。

设备缓存最近 16 条回执去重，不构成跨重启持久化幂等。USB 使用 UART0/stdin/stdout，禁止同时用 mpremote / Thonny 占用，上传前应释放 bridge 连接。

WebSocket 首包为 `snapshot`（BridgeState），随后为 `event`（DeviceEvent）或 `connection`。归并器保留最新 OLED / pins / status 和最多 200 条日志。慢客户端超过缓冲上限会断开，重连获取新快照。

固件 GPIO 快照按 6 项分片，每片均为 `partial=true`。主机在 bootId / 会话变化时清空，再按 GPIO 合并；同次启动刷新时保留此前已知记录，避免坏片使完整引脚表被首片清空。
