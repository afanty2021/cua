# cua-computer 模块文档

[根目录](../../CLAUDE.md) > [libs/python](../) > **computer**

> **最后更新**: 2026-05-01 | **版本**: 0.5.18 | **状态**: 生产就绪

---

## 模块职责

`cua-computer` 是 Cua 框架的**跨平台计算机控制接口层**，提供：

1. **统一抽象**: 屏蔽操作系统差异（Linux、macOS、Windows、Android）
2. **运行时无关**: 支持本地、Docker、QEMU、云端 VM
3. **异步优先**: 全异步 API 设计，高并发场景优化
4. **可观测性**: 内置日志、遥测、性能监控

---

## 入口与启动

### 核心类

```python
from computer import Computer

# 创建计算机实例
async with Computer(
    provider="docker",  # 或 "qemu", "cloud", "lume"
    image="trycua/cua-ubuntu:latest",
    name="my-computer"
) as comp:
    # 计算机已启动并连接
    screenshot = await comp.screenshot()
    print(f"屏幕尺寸: {screenshot.size}")
```

### 支持的运行时

| 运行时 | Provider 值 | 本地支持 | 云端支持 |
|--------|------------|---------|---------|
| **Docker** | `"docker"` | ✅ | ✅ |
| **QEMU** | `"qemu"` | ✅ | ✅ |
| **Lume** | `"lume"` | ✅ (macOS only) | ❌ |
| **Hyper-V** | `"hyperv"` | ✅ (Windows only) | ❌ |
| **Cloud** | `"cloud"` | ❌ | ✅ |

---

## 对外接口

### 计算机接口 (Computer Interface)

#### BaseComputerInterface

所有平台特定的接口基类：

```python
from computer.interface import BaseComputerInterface

class CustomInterface(BaseComputerInterface):
    async def screenshot(self) -> Image:
        """捕获屏幕截图"""
        pass

    async def mouse_move(self, x: int, y: int) -> None:
        """移动鼠标"""
        pass
```

#### 平台特定接口

```python
from computer.interface import (
    LinuxComputerInterface,
    MacOSComputerInterface,
    WindowsComputerInterface,
    AndroidComputerInterface
)

# 接口由 Computer 类根据平台自动选择
```

### 核心方法

#### 屏幕操作

```python
# 截图
screenshot = await comp.screenshot()
screenshot.save("screen.png")

# 获取屏幕尺寸
size = await comp.screen_size()
print(f"宽度: {size.width}, 高度: {size.height}")
```

#### 鼠标操作

```python
from computer.interface import MouseButton

# 移动鼠标
await comp.mouse_move(100, 200)

# 点击
await comp.mouse_click(MouseButton.LEFT)

# 拖拽
await comp.mouse_drag(start=(100, 100), end=(200, 200))

# 滚动
await comp.mouse_scroll(dx=0, dy=-5)
```

#### 键盘操作

```python
# 输入文本
await comp.keyboard_type("Hello, World!")

# 按键
await comp.keyboard_key("Enter")

# 组合键
await comp.keyboard_hotkey(["Command", "Shift", "4"])
```

#### Shell 操作

```python
# 执行命令
result = await comp.shell_run("ls -la")
print(result.stdout)
print(result.returncode)

# 交互式 Shell
async with comp.shell_interactive() as shell:
    await shell.send("python3")
    await shell.send("print('hello')")
    response = await shell.read()
```

---

## 关键依赖与配置

### 依赖项

```toml
[dependencies]
pillow>=10.0.0          # 图像处理
websocket-client>=1.8.0 # WebSocket 通信
websockets>=12.0        # 异步 WebSocket
aiohttp>=3.9.0          # HTTP 客户端
cua-core>=0.3.0         # 核心工具
pydantic>=2.11.1        # 数据验证
mslex>=1.3.0            # Windows 路径处理
```

### 配置选项

```python
from computer import Computer

comp = Computer(
    provider="docker",
    image="ubuntu:22.04",
    name="my-computer",

    # 性能配置
    screenshot_timeout=30,
    command_timeout=120,

    # 日志配置
    log_level="INFO",

    # 遥测配置
    telemetry_enabled=True,

    # 平台特定配置
    platform_config={
        "memory": "4GB",
        "cpus": 2
    }
)
```

---

## 数据模型

### Computer 模型

```python
from computer.models import Computer, ComputerState

class Computer:
    id: str
    name: str
    provider: VMProviderType
    state: ComputerState  # IDLE, RUNNING, STOPPED, ERROR
    platform: str  # "linux", "macos", "windows", "android"

    async def start(self) -> None:
        """启动计算机"""

    async def stop(self) -> None:
        """停止计算机"""

    async def restart(self) -> None:
        """重启计算机"""
```

### 接口数据模型

```python
from pydantic import BaseModel

class Screenshot(BaseModel):
    data: bytes  # PNG 格式图像数据
    size: tuple[int, int]  # (宽度, 高度)
    timestamp: datetime

class CommandResult(BaseModel):
    stdout: str
    stderr: str
    returncode: int
    duration: float
```

---

## 测试与质量

### 测试结构

```
libs/python/computer/tests/
├── test_computer.py         # Computer 类核心功能
├── test_interfaces.py       # 接口实现测试
├── test_providers.py        # Provider 测试
├── test_integration.py      # 集成测试
└── fixtures/
    ├── test_images.py       # 测试图像
    └── test_commands.py     # 测试命令
```

### 运行测试

```bash
cd libs/python/computer

# 运行所有测试
pytest tests/ -v

# 运行特定测试
pytest tests/test_computer.py::TestComputerContextManager -v

# 带覆盖率
pytest tests/ --cov=computer --cov-report=html
```

### 代码质量工具

```bash
# 格式化
black computer/

# Linting
ruff check computer/

# 类型检查
mypy computer/
```

---

## 常见问题 (FAQ)

### Q1: 如何处理平台特定的功能？

```python
from computer import Computer
from computer.interface import LinuxComputerInterface

async with Computer(provider="docker") as comp:
    if isinstance(comp.interface, LinuxComputerInterface):
        # Linux 特定功能
        await comp.interface.shell_run("apt-get update")
```

### Q2: 如何调试连接问题？

```python
import logging
logging.basicConfig(level=logging.DEBUG)

# 启用详细日志
comp = Computer(provider="docker", debug=True)
```

### Q3: 如何处理超时？

```python
try:
    result = await comp.shell_run("sleep 100", timeout=5)
except asyncio.TimeoutError:
    print("命令超时")
```

---

## 相关文件清单

### 核心文件

- `computer/__init__.py` - 模块入口
- `computer/computer.py` - Computer 类
- `computer/models.py` - 数据模型
- `computer/logger.py` - 日志配置
- `computer/tracing.py` - 性能追踪

### 接口文件

- `computer/interface/base.py` - 基础接口
- `computer/interface/generic.py` - 通用接口
- `computer/interface/linux.py` - Linux 接口
- `computer/interface/macos.py` - macOS 接口
- `computer/interface/windows.py` - Windows 接口
- `computer/interface/android.py` - Android 接口

### Provider 文件

- `computer/providers/base.py` - 基础 Provider
- `computer/providers/docker.py` - Docker Provider
- `computer/providers/qemu.py` - QEMU Provider
- `computer/providers/winsandbox.py` - Windows Sandbox Provider

### 工具文件

- `computer/pty.py` - PTY 客户端
- `computer/diorama_computer.py` - Diorama 集成

---

## 变更记录 (Changelog)

### 0.5.18 (当前版本)
- 改进 Windows 接口稳定性
- 添加 Android 接口支持
- 优化截图性能

### 历史版本
- 0.5.0: 重构接口系统
- 0.4.0: 添加遥测支持
- 0.1.0: 初始版本

---

*本文档由 AI 自动生成，反映模块当前状态。如有疑问，请参考源代码或提交 Issue。*
