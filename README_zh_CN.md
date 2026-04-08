# One-ESP 项目

[English](./README.md) | 简体中文

### @description: ESP32-WROOM 的 MicroPython 代码合集

**作者**: Fred Zhang Qi  
**日期**: 2024-05-23

本项目是一个基于 MicroPython 的 ESP32-WROOM 综合应用代码库。它集成了多种功能模块，包括网络连接、时间同步、屏幕显示以及与外部设备（如 Modbus 传感器）的通信。

## 功能特性 (Features)

- **WiFi 连接**: 自动连接预设的网络（通过读取 `.env` 配置文件）。
- **时间同步 (NTP)**: 支持从阿里云、腾讯云、Apple 等公共 NTP 服务器同步时间，实现精准的时间显示。
- **OLED 屏幕显示**: 实时显示设备连接状态、当前时间、以及传感器数据等。
- **Modbus 通信**: 通过 UART 串口与 Modbus 从机（如温湿度传感器）进行通信，读取并解析温度和湿度数据。
- **网络测试 (Socket)**: 提供基本的 Socket 请求功能，用于检验网络连通性。

## 项目结构 (Project Structure)

- `boot.py`: 上电启动脚本，自动引入并运行主程序。
- `application/main.py`: 主应用逻辑，串联并编排各个功能模块。
- `components/`: 核心功能组件目录
  - `env_reader.py`: 读取 `.env` 环境变量配置。
  - `wifi_connector.py`: 处理 WiFi 网络的扫描与连接逻辑。
  - `screen_display.py`: 控制屏幕的内容清除与显示。
  - `socket_test.py`: 提供 Socket 连通性测试模块。
  - `time_sync.py`: NTP 时间同步功能，并提供本地时间格式化。
  - `modbus_talker.py`: 负责与串口 Modbus 设备的通信与数据解析。
  - `device_addr_scanner.py`: 设备地址扫描器。

## 运行与部署方法

1. **准备配置文件**: 在项目根目录下创建 `.env` 文件，输入您的 WiFi 配置：
   ```env
   SSID=您的WiFi名称
   PASSWORD=您的WiFi密码
   ```
2. **下载 IDE**: 推荐下载并安装 MicroPython 的专用 IDE：[Thonny](https://thonny.org/)。
3. **连接开发板**: 将 ESP32-WROOM 开发板通过 USB 数据线连接至电脑，如果电脑无法识别，请安装对应的串口驱动（如 CP210x）。
4. **安装固件**: 打开 Thonny，配置并安装最新版的 MicroPython 固件（菜单操作：运行 -> 配置解释器 -> 安装或更新 MicroPython (esptool)）。
5. **上传代码**: 将项目的所有文件内容完整地上传至 ESP32 开发板。
6. **启动程序**: 在 Thonny 界面中直接运行 `main.py`，或给开发板重新上电，系统将通过 `boot.py` 自动启动主应用。
