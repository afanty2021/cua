# cua-driver 模块文档

[根目录](../../CLAUDE.md) > [libs](../) > **cua-driver**

> **最后更新**: 2026-05-01 | **版本**: 0.0.13 | **状态**: 生产就绪**

---

## 模块职责

`cua-driver` 是 **macOS 后台计算机使用驱动**，提供：

1. **后台操作**: 不抢占焦点、光标、空间的自动化
2. **AX 增强**: 超越标准 Accessibility API 的能力
3. **Chromium 支持**: 直接操作 Web 内容和 Canvas
4. **MCP 服务器**: 通过 stdio 与 Claude Code、Cursor 集成
5. **轨迹录制**: 所有操作可回放和分析

---

## 入口与启动

### 安装

```bash
# 使用安装脚本（推荐）
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"

# 手动安装
git clone https://github.com/trycua/cua.git
cd cua/libs/cua-driver
swift build
swift install
```

### 基本使用

```bash
# 启动 MCP 服务器
cua-driver mcp

# 在 Claude Code 中使用
# 1. 打开 Claude Code 设置
# 2. 添加 MCP 服务器配置
# 3. 服务器命令: cua-driver mcp
```

---

## 对外接口

### MCP 工具

#### 核心工具

```json
{
  "tools": [
    {
      "name": "computer__screenshot",
      "description": "捕获屏幕截图",
      "parameters": {
        "type": "object",
        "properties": {
          "coordinate_space": {
            "type": "string",
            "enum": ["screen", "window"]
          }
        }
      }
    },
    {
      "name": "computer__mouse_move",
      "description": "移动鼠标",
      "parameters": {
        "type": "object",
        "properties": {
          "x": {"type": "number"},
          "y": {"type": "number"}
        },
        "required": ["x", "y"]
      }
    },
    {
      "name": "computer__mouse_click",
      "description": "点击鼠标",
      "parameters": {
        "type": "object",
        "properties": {
          "button": {
            "type": "string",
            "enum": ["left", "middle", "right"]
          }
        }
      }
    },
    {
      "name": "computer__keyboard_type",
      "description": "输入文本",
      "parameters": {
        "type": "object",
        "properties": {
          "text": {"type": "string"}
        },
        "required": ["text"]
      }
    }
  ]
}
```

#### 高级工具

```bash
# 拖拽操作
computer__drag

# 滚动操作
computer__scroll

# 坐标转换
computer__coordinate_space

# 元素查找
computer__find_element

# 窗口管理
computer__window_focus
computer__window_list
```

### CLI 命令

```bash
# MCP 服务器模式
cua-driver mcp [--debug]

# 录制模式
cua-driver record --output trajectory.json

# 回放模式
cua-driver replay --input trajectory.json

# 版本信息
cua-driver --version
```

---

## 关键依赖与配置

### 系统要求

- **macOS**: 12.0 (Monterey) 或更高版本
- **权限**: 辅助功能权限（必需）
- **架构**: x86_64 或 arm64 (Apple Silicon)

### Swift 包依赖

```swift
// Package.swift
.dependencies: [
    .package(url: "https://github.com/apple/swift-argument-parser", from: "1.0.0"),
    .package(url: "https://github.com/apple/swift-log", from: "1.0.0")
]
```

### 配置文件

```json
{
  "driver": {
    "debug": false,
    "logLevel": "info",
    "telemetryEnabled": true
  },
  "mcp": {
    "serverName": "cua-driver",
    "timeout": 30
  },
  "recording": {
    "outputPath": "./trajectories",
    "includeScreenshots": true
  }
}
```

---

## 数据模型

### 轨迹格式

```json
{
  "version": "1.0",
  "metadata": {
    "recorded_at": "2026-05-01T22:49:22Z",
    "macos_version": "15.4",
    "driver_version": "0.0.13"
  },
  "actions": [
    {
      "type": "screenshot",
      "timestamp": 1680000000.000,
      "data": "base64_encoded_image"
    },
    {
      "type": "mouse_move",
      "timestamp": 1680000001.000,
      "x": 100,
      "y": 200
    },
    {
      "type": "mouse_click",
      "timestamp": 1680000002.000,
      "button": "left"
    }
  ]
}
```

---

## 测试与质量

### 测试结构

```
libs/cua-driver/Tests/
├── CuaDriverCoreTests/
│   ├── AXDriverTests.swift
│   ├── ChromiumTests.swift
│   └── MouseTests.swift
└── CuaDriverServerTests/
    ├── MCPServerTests.swift
    └── ToolValidationTests.swift
```

### 运行测试

```bash
cd libs/cua-driver

# 运行所有测试
swift test

# 运行特定测试
swift test --filter AXDriverTests

# 生成测试覆盖率
swift test --enable-code-coverage
```

---

## 常见问题 (FAQ)

### Q1: 如何授予辅助功能权限？

1. 打开"系统设置" → "隐私与安全性" → "辅助功能"
2. 找到 `cua-driver` 并勾选
3. 重新启动驱动

### Q2: 如何调试驱动行为？

```bash
# 启用调试模式
cua-driver mcp --debug

# 查看日志
log stream --predicate 'process == "cua-driver"'
```

### Q3: 如何与特定应用交互？

```python
# 通过 MCP 调用
tools.computer__window_focus("Safari")
tools.computer__mouse_click(x=100, y=200)
```

### Q4: 支持哪些应用？

- ✅ **原生 macOS 应用**: 通过 AX API
- ✅ **Chromium 浏览器**: Chrome、Edge、Brave
- ✅ **Canvas 应用**: Blender、Figma、游戏引擎
- ⚠️ **Electron 应用**: 部分支持

---

## 相关文件清单

### 核心文件

- `Sources/CuaDriverCore/main.swift` - 入口点
- `Sources/CuaDriverCore/AXDriver.swift` - AX 驱动
- `Sources/CuaDriverCore/ChromiumDriver.swift` - Chromium 驱动
- `Sources/CuaDriverCore/Recorder.swift` - 轨迹录制

### 服务器文件

- `Sources/CuaDriverServer/Server.swift` - MCP 服务器
- `Sources/CuaDriverServer/Tools/` - 工具实现

### 文档文件

- `README.md` - 主文档
- `Skills/cua-driver/README.md` - Claude Code Skill 文档
- `Skills/cua-driver/TESTS.md` - 测试文档
- `Skills/cua-driver/WEB_APPS.md` - Web 应用支持

---

## 变更记录 (Changelog)

### 0.0.13 (当前版本)
- 添加拖拽工具支持
- 改进 Chromium 驱动稳定性
- 修复坐标转换问题

### 历史版本
- 0.0.12: 添加轨迹录制
- 0.0.10: 初始 MCP 支持
- 0.0.1: 初始版本

---

*本文档由 AI 自动生成，反映模块当前状态。如有疑问，请参考源代码或提交 Issue。*
