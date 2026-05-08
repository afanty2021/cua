# cua-sandbox 模块文档

[根目录](../../../CLAUDE.md) > [libs/python](../) > **cua-sandbox**

> **最后更新**: 2026-05-01 | **版本**: 0.5.x | **状态**: 生产就绪

---

## 模块职责

`cua-sandbox` 是 Cua 框架的**沙箱运行时管理层**，提供：

1. **多云支持**: Docker、QEMU、Lume、云端统一接口
2. **运行时抽象**: `Runtime` 基类屏蔽底层差异
3. **镜像管理**: 跨平台镜像构建与分发
4. **传输层**: HTTP、WebSocket、stdio 多协议支持
5. **会话管理**: 并发会话、资源池、生命周期管理

---

## 入口与启动

### 核心类

```python
from cua_sandbox import Sandbox, Image

# 创建临时沙箱（自动清理）
async with Sandbox.ephemeral(Image.linux()) as sb:
    result = await sb.shell.run("uname -a")
    print(result.stdout)

# 沙箱自动销毁
```

### 支持的运行时

| 运行时 | 本地支持 | 云端支持 | 启动速度 | 适用场景 |
|--------|---------|---------|---------|---------|
| **Docker** | ✅ | ✅ | ⚡ 极快 | 容器化应用、CI/CD |
| **QEMU** | ✅ | ✅ | 🐢 慢 | 完整 OS、Android、Windows |
| **Lume** | ✅ (macOS) | ❌ | 🚀 快 | macOS 虚拟化 |
| **Hyper-V** | ✅ (Windows) | ❌ | 🚀 快 | Windows 虚拟化 |
| **Cloud** | ❌ | ✅ | ⚡ 快 | 云端托管、GPU 加速 |

---

## 对外接口

### Sandbox 类

#### 创建沙箱

```python
from cua_sandbox import Sandbox, Image

# 1. 临时沙箱（推荐）
async with Sandbox.ephemeral(
    image=Image.linux(),
    local=True  # 本地运行
) as sb:
    # 使用沙箱
    await sb.shell.run("echo hello")

# 2. 持久化沙箱
sb = await Sandbox.create(
    image=Image.windows(),
    name="my-windows-sandbox",
    local=True
)
try:
    # 使用沙箱
    await sb.shell.run("dir C:\\")
finally:
    await sb.destroy()

# 3. 云端沙箱
async with Sandbox.ephemeral(
    image=Image.linux(),
    local=False  # 云端运行
) as sb:
    await sb.shell.run("nvidia-smi")  # GPU 加速
```

#### 沙箱配置

```python
from cua_sandbox import Sandbox, Image

sb = await Sandbox.create(
    image=Image.linux(),

    # 资源配置
    memory="4GB",
    cpus=2,

    # 网络配置
    port_mappings={8080: 80},  # 主机端口:容器端口

    # 环境变量
    env_vars={
        "MY_VAR": "value",
        "PATH": "/usr/local/bin:/usr/bin"
    },

    # 挂载卷
    volumes={
        "/host/path": "/container/path"
    }
)
```

### Image 类

#### 预定义镜像

```python
from cua_sandbox import Image

# Linux
Image.linux()  # Ubuntu 22.04
Image.ubuntu(version="22.04")
Image.debian()

# macOS
Image.macos(version="sequoia")

# Windows
Image.windows(version="11")

# Android
Image.android(version="14")

# 自定义镜像
Image.from_name("custom-image:latest")
Image.from_dockerfile("Dockerfile")
```

#### 镜像操作

```python
# 列出本地镜像
images = await Image.list()

# 拉取镜像
image = await Image.pull("trycua/cua-ubuntu:latest")

# 构建镜像
image = await Image.build(
    dockerfile="Dockerfile",
    context=".",
    tag="my-image:latest"
)

# 删除镜像
await image.delete()
```

### 接口系统

```python
from cua_sandbox import Sandbox

async with Sandbox.ephemeral(Image.linux()) as sb:
    # Shell 接口
    result = await sb.shell.run("ls -la")
    print(result.stdout)

    # Mouse 接口
    await sb.mouse.move(100, 200)
    await sb.mouse.click()

    # Keyboard 接口
    await sb.keyboard.type("Hello, World!")

    # Screen 接口
    screenshot = await sb.screen.screenshot()
    screenshot.save("screen.png")

    # Clipboard 接口
    await sb.clipboard.write("text")
    text = await sb.clipboard.read()

    # Terminal 接口
    async with sb.terminal.start() as term:
        await term.send("python3")
        output = await term.read()
```

---

## 关键依赖与配置

### 依赖项

```toml
[dependencies]
cua-core>=0.3.0       # 核心工具
aiohttp>=3.9.0        # HTTP 客户端
websockets>=12.0      # WebSocket
pydantic>=2.11.1      # 数据验证
```

### 运行时特定依赖

```toml
[extras]
# Docker 支持
docker = [
    "docker>=7.0.0"
]

# QEMU 支持
qemu = [
    "qemu-disk>=0.1.0",
    "vncdotool>=1.0.0"
]

# Lume 支持
lume = [
    "lume>=0.2.0"
]
```

### 配置选项

```python
import cua_sandbox

# 全局配置
cua_sandbox.configure(
    api_key="cua-xxx",
    base_url="https://api.cua.ai",
    default_runtime="docker",
    telemetry_enabled=False
)

# 沙箱特定配置
sb = await Sandbox.create(
    image=Image.linux(),
    runtime="docker",  # 覆盖默认运行时
    timeout=300,       # 默认超时
    debug=True         # 调试模式
)
```

---

## 数据模型

### SandboxInfo

```python
from cua_sandbox import SandboxInfo

class SandboxInfo(BaseModel):
    id: str
    name: str
    state: SandboxState  # RUNNING, STOPPED, ERROR
    image: str
    runtime: str
    created_at: datetime
    uptime: timedelta
    ports: dict[int, int]  # 端口映射
```

### ImageInfo

```python
class ImageInfo(BaseModel):
    name: str
    tag: str
    size: int  # 字节
    created_at: datetime
    os: str  # "linux", "macos", "windows", "android"
    architecture: str  # "x86_64", "arm64"
```

---

## 测试与质量

### 测试结构

```
libs/python/cua-sandbox/tests/
├── test_sandbox.py         # Sandbox 核心功能
├── test_runtime.py         # Runtime 测试
├── test_interfaces.py      # 接口测试
├── test_cloud.py           # 云端集成测试
├── test_config.py          # 配置测试
└── fixtures/
    ├── test_images.py      # 测试镜像
    └── mock_server.py      # Mock 服务器
```

### 运行测试

```bash
cd libs/python/cua-sandbox

# 运行所有测试
pytest tests/ -v

# 运行特定运行时测试
pytest tests/test_runtime.py::test_docker_runtime -v

# 跳过云端测试（需要 API 密钥）
pytest tests/ -v -m "not cloud"

# 带覆盖率
pytest tests/ --cov=cua_sandbox --cov-report=html
```

### Mock 测试示例

```python
import pytest
from unittest.mock import AsyncMock, patch
from cua_sandbox import Sandbox, Image

@pytest.mark.asyncio
async def test_sandbox_creation():
    with patch("cua_sandbox.runtime.docker.DockerRuntime.create") as mock_create:
        mock_create.return_value = AsyncMock()

        sb = await Sandbox.create(Image.linux())
        assert sb is not None
```

---

## 常见问题 (FAQ)

### Q1: 如何选择合适的运行时？

```python
from cua_sandbox import Sandbox, Image

# 容器化应用 - Docker
async with Sandbox.ephemeral(Image.linux(), runtime="docker") as sb:
    pass  # 快速启动

# 完整 OS - QEMU
async with Sandbox.ephemeral(Image.windows(), runtime="qemu") as sb:
    pass  # 完整 Windows 体验

# macOS 虚拟化 - Lume
async with Sandbox.ephemeral(Image.macos(), runtime="lume") as sb:
    pass  # 原生 macOS
```

### Q2: 如何处理沙箱超时？

```python
import asyncio
from cua_sandbox import Sandbox, Image

try:
    async with asyncio.timeout(300):  # 5 分钟超时
        async with Sandbox.ephemeral(Image.linux()) as sb:
            await sb.shell.run("long-running-command")
except asyncio.TimeoutError:
    print("操作超时")
```

### Q3: 如何调试沙箱问题？

```python
import logging
from cua_sandbox import Sandbox, Image

# 启用详细日志
logging.basicConfig(level=logging.DEBUG)

# 创建沙箱时启用调试
sb = await Sandbox.create(
    Image.linux(),
    debug=True,
    log_level="DEBUG"
)
```

### Q4: 如何清理僵尸沙箱？

```python
from cua_sandbox import Sandbox

# 列出所有沙箱
sandboxes = await Sandbox.list()

# 清理已停止的沙箱
for sb in sandboxes:
    if sb.state == SandboxState.STOPPED:
        await sb.destroy()

# 强制清理所有
await Sandbox.prune()
```

---

## 相关文件清单

### 核心文件

- `cua_sandbox/__init__.py` - 模块入口
- `cua_sandbox/sandbox.py` - Sandbox 类
- `cua_sandbox/image.py` - Image 类

### Runtime 实现

- `cua_sandbox/runtime/base.py` - 基础 Runtime
- `cua_sandbox/runtime/docker.py` - Docker Runtime
- `cua_sandbox/runtime/qemu.py` - QEMU Runtime
- `cua_sandbox/runtime/lume.py` - Lume Runtime
- `cua_sandbox/runtime/android_emulator.py` - Android Emulator
- `cua_sandbox/runtime/hyperv.py` - Hyper-V Runtime

### 接口实现

- `cua_sandbox/interfaces/shell.py` - Shell 接口
- `cua_sandbox/interfaces/mouse.py` - Mouse 接口
- `cua_sandbox/interfaces/keyboard.py` - Keyboard 接口
- `cua_sandbox/interfaces/screen.py` - Screen 接口
- `cua_sandbox/interfaces/clipboard.py` - Clipboard 接口
- `cua_sandbox/interfaces/terminal.py` - Terminal 接口

### 传输层

- `cua_sandbox/transport/http.py` - HTTP 传输
- `cua_sandbox/transport/websocket.py` - WebSocket 传输
- `cua_sandbox/transport/stdio.py` - Stdio 传输

### 配置与工具

- `cua_sandbox/_config.py` - 配置管理
- `cua_sandbox/runtime/compat.py` - 运行时兼容性

---

## 变更记录 (Changelog)

### 当前版本 (0.5.x)
- 添加 Hyper-V Runtime 支持
- 改进 Android Emulator 集成
- 优化并发会话管理
- 修复 Docker 网络问题

### 历史版本
- 0.4.0: 添加 Lume Runtime
- 0.3.0: 重构接口系统
- 0.2.0: 添加云端支持
- 0.1.0: 初始版本

---

*本文档由 AI 自动生成，反映模块当前状态。如有疑问，请参考源代码或提交 Issue。*
