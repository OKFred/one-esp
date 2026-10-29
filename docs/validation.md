# 验证记录

环境：Windows、Node.js 26.13.0、pnpm 11.5.0、ESP32-WROOM-32 / MicroPython 1.22.2、CH340 USB→TTL / COM3 / 115200 8N1。开发板传感器接线已全部断开。

## 自动检查

- TypeScript / Vue 类型检查、Oxlint + Vue ESLint、Oxfmt、生产构建通过。
- 62 项协议 / bridge / Vue 测试通过；包括 ACK 阶段、重启取消、字符串配置转换、秘密过滤、串口重连、慢连接、UTF-8 分片和模拟时钟走时。
- 69 项固件主机测试通过；包括时钟 float32 精度、慢串口下 OLED 优先级、分钟 TCP 调度与非阻塞连接、ESP32 errno 兼容、Wi-Fi 失败、事务恢复、有界日志与 TTL。
- Playwright 在 390 / 768 / 1440 宽度共 3 项场景测试通过：正常 / Wi-Fi 错误 / 离线、OLED 实际亮像素与 2:1 比例、配置弹窗、debug 开关、默认 GPIO 禁写、无整体横向溢出、零 console / pageerror。
- 浏览器截图生成于 Git 忽略的 `test-results/screenshots/`。回放截图属于模拟数据。

## 实机验证

- 完整设备备份保存在 Git 忽略的 `.device-backups/`，包含设备私有配置；上传前逐文件 SHA-256 校验。原始 MicroPython 设备文件没有发布到仓库。
- 固件上传后正常连接原 Wi-Fi、同步 NTP、完成 TCP 测试，传感器始终禁用。
- 连接已运行且 debug 关闭的设备，bridge 自动发送 LF + snapshot.get，取得真实 OLED 1024 字节帧缓冲与 34 项 GPIO 元数据；GPIO 写白名单为空。
- FULL 短会话自动过期，关闭后调试状态同步；持续采集未出现重启、内存异常或协议解析错误。一次 20 状态样本采集中可用堆内存为 41,600–106,752 字节；这是短时功能验证，不是长时稳定性保证。
- 不可达 SSID 的 60 秒试连返回 rolled-back，原配置 revision 不变，并恢复原网络连接。USB 在试连和离线过程中继续提供状态、屏幕和命令回执。
- 完整配置表单按原值保存收到 applied，revision 递增，密码保持配置且不回读。实机发现并修正 MicroPython 1.22 不支持 str.isalnum 的兼容问题，域名使用 ASCII 范围校验。
- 最终固件与 bridge 再次实机检查通过：21 个状态样本、3 个 OLED 帧、完整配置保存、自动到期和关闭，无坏帧告警或异常重启；可用堆内存 41,328–102,720 字节。
- 当前 USB→TTL 链路曾出现坏帧，保留告警并有界重取快照（2 秒冷却、每连接最多 3 次）；不自动重发配置或重启操作。Host 回归覆盖 28 项残缺引脚状态经恢复重新变为 34 项。
- 时钟与分钟探测后续实测：FULL debug 下收到 68 个秒数不同的 OLED 帧、71 个状态样本；TCP 发起 uptime 为 4,317 和 64,365ms，间隔 60,048ms，成功连接目标。可用堆内存 39,792–98,304 字节；出现 3 次可恢复坏帧告警，最终 34 项引脚完整且没有异常重启。验证后 debug 正常关闭。
- 时钟改为先计算整数时区偏移，再与整数 epoch 相加，避免 ESP32 float32 把当前时间量化成约 64 秒步长。非阻塞 TCP 的完成确认兼容 ESP32 未导出的 newlib `EISCONN=127`；该值已通过真实设备调用确认。

## 发现并处理的兼容边界

Windows 的 SerialPort 13 原生绑定在 Node.js 26.4+ 可能停滞 Promise / nextTick 回调，参见上游 [bindings-cpp #243](https://github.com/serialport/bindings-cpp/pull/243)。本机 A/B：没有唤醒任务时 3 秒后 write callback 未完成；20ms 唤醒后 callback 返回且收到 9,797 字节。当前仅在受影响 Windows 串口打开期间启用 unref 唤醒任务，关闭清理。保留 Node 26 LTS，没有切换系统默认 Node。

ESP32 UART0 接收环形缓冲小，设备发送 OLED 时会同步占用时间。Host 命令按 UTF-8 每片最多 64 字节、间隔 60ms 完整串行发送；固件每 tick 最多一帧，避免大表单或并发命令互相交错及丢字节。这一限制与 Node 版本无关。

同步 DNS 仍可能暂时延迟 USB；TCP 缓存解析结果，并以非阻塞轮询连接，debug 开启仅暂停 NTP。未知 GPIO 没有电气测量，输出白名单为空。构建主包约 609KB / gzip 195KB，有体积提示；本期没有云部署、MQTT 实机验证或温湿度采集。

可选实机检查：本地 bridge 运行后执行 `node tools/hardware-smoke.mjs`。它会开启短期 debug、按原值保存完整配置（revision 加一），验证多帧时钟变化后关闭 debug；加 `--wifi-rollback` 测试不可达 SSID 试用回退，加 `--minute-probe` 连续观察 65 秒、核对两次 TCP 发起间隔和成功结果。它不是 CI 测试，执行时独占设备控制，不要同时操作网页或上传。
