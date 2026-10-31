# 直接依赖清单

版本与用途按工作区清单组织；Node.js 依赖的传递版本和完整性哈希见 `pnpm-lock.yaml`，主机 Python 依赖见 `uv.lock`。运行时 Node.js 26.13.0 / pnpm 11.5.0；主机 CPython 3.14.2 由 `.python-version` 固定，uv 按根目录 `pyproject.toml` 和锁文件管理项目 `.venv/`。当前设备运行 MicroPython 1.29.0 正式冻结修复镜像，私有配置保留；业务 TLS、RTU CRC、环境 PUBACK 及限定的 MQTT 配置命令已有独立运行证据，USB 链路仍有拒收坏帧，服务端踢线及长期稳定性仍待验收，见 [验证记录](validation.md)。仓库的 21 个运行源码已包含 `lib/ssd1306.py`，无需另计一个驱动文件。

## 主机 Python 工具

| 包          | 固定版本               | 用途                                                                                                                           |
| ----------- | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `mpremote`  | `1.28.0`               | MicroPython 设备备份与上传                                                                                                     |
| `mpy-cross` | `1.29.0.post2`（默认） | `--precompile` 通过隔离的 `uv tool run --from` 调用；实际编译器为 MicroPython 1.29.0，输出 MPY 6.3 纯字节码，不装入全局 Python |
| `mpy-cross` | `1.22.2`（旧固件回退） | 显式指定 `--mpy-version 1.22.2`，为 MicroPython 1.22.2 生成 MPY 6.2；该参数不刷写或降级固件                                    |

Python 项目支持 `>=3.14,<3.15`，默认使用固定的 3.14.2；本次迁移使用 uv 0.9.28。`pyserial`、`platformdirs` 等传递依赖的版本与文件哈希由 `uv.lock` 固定。安装执行 `uv sync --locked`，工具和测试执行 `uv run --locked`；依赖保持在项目 `.venv/`，不安装到全局 Python。该环境只服务主机工具与测试，不上传到 ESP32。

## one-esp

| 包                          | 固定版本                            | 范围        |
| --------------------------- | ----------------------------------- | ----------- |
| `@playwright/test`          | `1.64.1`                            | 开发 / 校验 |
| `@types/node`               | `26.6.6`                            | 开发 / 校验 |
| `@typescript/native`        | `npm:typescript@7.0.2`              | 开发 / 校验 |
| `@typescript-eslint/parser` | `8.72.1`                            | 开发 / 校验 |
| `eslint`                    | `10.13.0`                           | 开发 / 校验 |
| `eslint-config-prettier`    | `10.1.8`                            | 开发 / 校验 |
| `eslint-plugin-vue`         | `10.12.0`                           | 开发 / 校验 |
| `jsdom`                     | `30.1.3`                            | 开发 / 校验 |
| `oxfmt`                     | `0.75.0`                            | 开发 / 校验 |
| `oxlint`                    | `1.90.0`                            | 开发 / 校验 |
| `typescript`                | `npm:@typescript/typescript6@6.0.2` | 开发 / 校验 |
| `vitest`                    | `5.0.5`                             | 开发 / 校验 |
| `vue-eslint-parser`         | `10.4.1`                            | 开发 / 校验 |

## @one-esp/bridge

| 包                         | 固定版本      | 范围        |
| -------------------------- | ------------- | ----------- |
| `@hono/node-server`        | `2.1.5`       | 运行        |
| `@one-esp/protocol`        | `workspace:*` | 运行        |
| `hono`                     | `4.13.14`     | 运行        |
| `serialport`               | `13.0.0`      | 运行        |
| `ws`                       | `8.22.0`      | 运行        |
| `@serialport/binding-mock` | `10.2.2`      | 开发 / 校验 |
| `@types/ws`                | `8.18.2`      | 开发 / 校验 |
| `tsx`                      | `4.23.15`     | 开发 / 校验 |

## @one-esp/web

| 包                      | 固定版本      | 范围        |
| ----------------------- | ------------- | ----------- |
| `@iconify-icons/lucide` | `2.0.25`      | 运行        |
| `@iconify/vue`          | `5.0.4`       | 运行        |
| `@one-esp/protocol`     | `workspace:*` | 运行        |
| `ant-design-vue`        | `4.2.6`       | 运行        |
| `vue`                   | `3.5.44`      | 运行        |
| `@vitejs/plugin-vue`    | `6.0.10`      | 开发 / 校验 |
| `@vue/compiler-dom`     | `3.5.44`      | 开发 / 校验 |
| `@vue/test-utils`       | `2.5.1`       | 开发 / 校验 |
| `jsdom`                 | `30.1.3`      | 开发 / 校验 |
| `vite`                  | `8.3.5`       | 开发 / 校验 |
| `vue-tsc`               | `3.3.13`      | 开发 / 校验 |

## @one-esp/protocol

| 包    | 固定版本 | 范围 |
| ----- | -------- | ---- |
| `zod` | `4.6.7`  | 运行 |

## 工具边界

- Hono 提供 HTTP；`@hono/node-server` 和 `ws` 用于 Node 服务端 WebSocket；`serialport` 是本机原生串口模块。
- Zod 在主机校验共享协议；MicroPython 固件采用轻量手写校验，不安装 Node 依赖。
- Vue 3 与 Ant Design Vue 提供响应式页面和控件，Canvas 绘制屏幕，SVG 绘制板图；Iconify Lucide 图标静态打包。
- Go 原生 TS7 以 `@typescript/native` 别名安装为默认 tsc；Vue 工具链使用 TS6 兼容包。
- Oxlint / Oxfmt 使用原生实现，ESLint 只补 Vue 模板规则，不重复承担格式化。
- Playwright 浏览器、SerialPort bindings 和 Ox 系工具包含平台二进制；安装策略只允许已登记的必要构建脚本。
- MQTT 使用仓库内的轻量 MQTT 3.1.1 wire / transport / scheduler 和 MicroPython 内置 socket / ssl / _thread，不引入 MQTT.js 或第三方固件库。Wrangler、Workers 类型、数据库、状态库、图表库和 Rust 应用服务尚未引入；远程阶段按实际需要新增。
