# cua-agent 模块文档

[根目录](../../CLAUDE.md) > [libs/python](../) > **agent**

> **最后更新**: 2026-05-01 | **版本**: 0.8.2 | **状态**: 活跃开发中

---

## 模块职责

`cua-agent` 是 Cua 框架的**多模型计算机使用代理层**，提供：

1. **统一代理接口**: 抽象不同 VLM 模型的差异
2. **装饰器架构**: 使用 `@register_agent` 轻松添加新模型
3. **工具集成**: 自动将计算机接口转换为 LLM 工具
4. **多模态支持**: 文本、图像、音频输入输出
5. **循环管理**: 支持单步、多步、交互式执行

---

## 入口与启动

### 核心类

```python
from cua_agent import ComputerAgent

# 创建代理实例
agent = ComputerAgent(
    model="anthropic/claude-sonnet-4-5",
    tools=[computer],  # Computer 实例列表
    api_key="sk-ant-xxx"
)

# 运行代理
async for response in agent.run("打开浏览器并访问 example.com"):
    print(response.content)
```

### 支持的模型

| 模型 | 模型标识符 | 状态 |
|------|-----------|------|
| **Claude Sonnet 4.5** | `anthropic/claude-sonnet-4-5` | ✅ 稳定 |
| **Claude Opus 4** | `anthropic/claude-opus-4` | ✅ 稳定 |
| **GPT-4o** | `openai/gpt-4o` | ✅ 稳定 |
| **GPT-4o Computer Use** | `openai/computer-use-preview` | ✅ 稳定 |
| **Gemini 2.0 Flash** | `google/gemini-2.0-flash` | ✅ 稳定 |
| **Qwen 2.5-VL** | `qwen/qwen-2.5-vl` | ✅ 稳定 |
| **GLM-4V** | `glm/glm-4v` | ⚠️ 实验性 |
| **UiTARS** | `uitars` | 🔨 开发中 |

---

## 对外接口

### ComputerAgent 类

#### 初始化

```python
from cua_agent import ComputerAgent

agent = ComputerAgent(
    # 模型配置
    model="anthropic/claude-sonnet-4-5",

    # 工具配置
    tools=[computer1, computer2],

    # API 配置
    api_key="sk-xxx",
    base_url="https://api.anthropic.com",

    # 行为配置
    max_steps=100,
    timeout_per_step=120,

    # 遥测配置
    telemetry_enabled=True,

    # 自定义配置
    **kwargs
)
```

#### 运行方法

```python
# 1. 简单运行（单次响应）
response = await agent.run("打开计算器")

# 2. 流式运行（多步骤）
async for response in agent.run("计算 1+1"):
    print(response.content)
    if response.done:
        break

# 3. 交互式运行
async for response in agent.run_interactive("帮我编辑文件"):
    user_input = input("请输入: ")
    await agent.send(user_input)
```

### 响应类型

```python
from cua_agent import AgentResponse

class AgentResponse:
    content: str           # 文本内容
    images: list[Image]    # 图像列表
    done: bool            # 是否完成
    error: Exception | None  # 错误信息
    steps_taken: int      # 已执行步数
```

### 工具系统

#### 自动工具生成

```python
from cua_agent import ComputerAgent
from computer import Computer

computer = Computer(provider="docker")
agent = ComputerAgent(model="claude-sonnet-4-5", tools=[computer])

# 自动生成的工具：
# - computer_screenshot
# - computer_mouse_move
# - computer_mouse_click
# - computer_keyboard_type
# - computer_shell_run
```

#### 自定义工具

```python
from cua_agent import tool

@tool
def custom_calculator(a: int, b: int) -> int:
    """执行自定义计算"""
    return a + b

agent = ComputerAgent(
    model="claude-sonnet-4-5",
    tools=[computer, custom_calculator]
)
```

---

## 关键依赖与配置

### 依赖项

```toml
[dependencies]
httpx>=0.27.0           # HTTP 客户端
aiohttp>=3.9.3          # 异步 HTTP
anyio>=4.4.1            # 异步运行时
typing-extensions>=4.12.2
pydantic>=2.6.4         # 数据验证
rich>=13.7.1            # 终端输出
python-dotenv>=1.0.1    # 环境变量
cua-core>=0.3.0         # 核心工具
certifi>=2024.2.2       # SSL 证书
litellm==1.80.0         # 多模型接口
Pillow>=10.0.0          # 图像处理
```

### 可选依赖

```toml
[extras]
# OpenAI 模型
openai = []

# Anthropic 模型
anthropic = []

# Qwen 模型
qwen = [
    "qwen-vl-utils",
    "qwen-agent",
]

# Gemini 模型
gemini = [
    "google-genai>=1.41.0"
]

# SOM 视觉解析
omni = [
    "cua-som>=0.1.0"
]

# UiTARS (MLX)
uitars-mlx = [
    "mlx-vlm>=0.1.27; sys_platform == 'darwin'"
]

# 云端部署（不包含本地模型）
cloud = [
    "google-genai>=1.41.0",
    "yaspin>=3.1.0",
    "qwen-agent",
    "qwen-vl-utils",
]
```

### 配置示例

```python
from cua_agent import ComputerAgent
import os

# 从环境变量加载配置
agent = ComputerAgent(
    model=os.getenv("CUA_MODEL", "anthropic/claude-sonnet-4-5"),
    api_key=os.getenv("ANTHROPIC_API_KEY"),
    tools=[computer],

    # 性能配置
    max_steps=int(os.getenv("CUA_MAX_STEPS", "100")),
    timeout_per_step=int(os.getenv("CUA_STEP_TIMEOUT", "120")),

    # 遥测配置
    telemetry_enabled=os.getenv("CUA_TELEMETRY", "true").lower() == "true"
)
```

---

## 数据模型

### Agent Loop 配置

```python
from cua_agent.loops.base import AsyncAgentConfig

class AnthropicConfig(AsyncAgentConfig):
    model: str = "claude-sonnet-4-5"
    max_tokens: int = 8192
    temperature: float = 0.0

    async def run(self, prompt: str, tools: list) -> AgentResponse:
        """运行代理循环"""
        pass
```

### 消息格式

```python
from cua_agent import Messages

class Messages(BaseModel):
    messages: list[dict]  # 对话历史
    system_prompt: str    # 系统提示
    max_history: int = 10  # 保留的历史消息数

    def add_user_message(self, content: str) -> None:
        """添加用户消息"""

    def add_assistant_message(self, content: str) -> None:
        """添加助手消息"""
```

---

## 测试与质量

### 测试结构

```
libs/python/agent/tests/
├── test_agent.py           # ComputerAgent 核心功能
├── test_loops.py           # 各模型 Loop 测试
├── test_tools.py           # 工具系统测试
├── test_integration.py     # 集成测试
└── fixtures/
    ├── test_prompts.py     # 测试提示词
    └── mock_responses.py   # 模拟响应
```

### 运行测试

```bash
cd libs/python/agent

# 运行所有测试
pytest tests/ -v

# 运行特定模型测试
pytest tests/test_loops.py::test_anthropic_loop -v

# 跳过慢速测试
pytest tests/ -v -m "not slow"

# 带覆盖率
pytest tests/ --cov=cua_agent --cov-report=html
```

### Mock 测试示例

```python
import pytest
from unittest.mock import AsyncMock, patch
from cua_agent import ComputerAgent

@pytest.mark.asyncio
async def test_agent_run_with_mock():
    # Mock API 响应
    with patch("cua_agent.loops.anthropic.anthropic") as mock_anthropic:
        mock_anthropic.AsyncAnthropic.return_value = AsyncMock()

        agent = ComputerAgent(
            model="claude-sonnet-4-5",
            tools=[],
            api_key="test-key"
        )

        response = await agent.run("测试任务")
        assert response.content is not None
```

---

## 常见问题 (FAQ)

### Q1: 如何添加新的模型支持？

```python
from cua_agent import register_agent
from cua_agent.loops.base import AsyncAgentConfig

@register_agent("my-model")
class MyModelConfig(AsyncAgentConfig):
    model: str = "my-model-v1"

    async def run(self, prompt: str, tools: list) -> AgentResponse:
        # 实现模型调用逻辑
        response = await call_my_model_api(prompt, tools)
        return AgentResponse(content=response)

# 使用
agent = ComputerAgent(model="my-model", tools=[computer])
```

### Q2: 如何处理长任务？

```python
agent = ComputerAgent(
    model="claude-sonnet-4-5",
    tools=[computer],
    max_steps=500,  # 增加最大步数
    timeout_per_step=300  # 增加单步超时
)

# 使用检查点保存状态
async for response in agent.run("长任务"):
    if response.steps_taken % 100 == 0:
        # 保存检查点
        save_checkpoint(agent.state)
```

### Q3: 如何调试代理行为？

```python
import logging

# 启用详细日志
logging.basicConfig(level=logging.DEBUG)

# 使用 rich 库显示漂亮的输出
from rich.console import Console
console = Console()

async for response in agent.run("任务"):
    console.print(f"[green]步骤 {response.steps_taken}:[/green]")
    console.print(response.content)
```

### Q4: 如何处理 API 错误？

```python
from cua_agent import ComputerAgent
from anthropic import APIError

agent = ComputerAgent(model="claude-sonnet-4-5", tools=[computer])

try:
    response = await agent.run("任务")
except APIError as e:
    print(f"API 错误: {e}")
    # 重试逻辑
    response = await agent.run("任务")
```

---

## 相关文件清单

### 核心文件

- `cua_agent/__init__.py` - 模块入口
- `cua_agent/agent.py` - ComputerAgent 类
- `cua_agent/decorators.py` - 装饰器系统
- `cua_agent/types.py` - 类型定义
- `cua_agent/callbacks.py` - 回调函数

### Loop 实现

- `cua_agent/loops/__init__.py` - Loop 注册
- `cua_agent/loops/base.py` - 基础配置
- `cua_agent/loops/anthropic.py` - Anthropic Claude
- `cua_agent/loops/openai.py` - OpenAI GPT
- `cua_agent/loops/gemini.py` - Google Gemini
- `cua_agent/loops/qwen35.py` - Qwen 2.5-VL
- `cua_agent/loops/generic_vlm.py` - 通用 VLM

### 集成模块

- `cua_agent/integrations/hud/` - HUD 集成
- `cua_agent/integrations/vlm/` - VLM 模型集成

---

## 变更记录 (Changelog)

### 0.8.2 (当前版本)
- 添加 Gemini 2.0 Flash 支持
- 改进工具调用错误处理
- 优化多步任务性能

### 历史版本
- 0.8.0: 重构装饰器系统
- 0.7.0: 添加 Qwen 支持
- 0.6.0: 添加 OpenAI Computer Use
- 0.5.0: 初始版本

---

*本文档由 AI 自动生成，反映模块当前状态。如有疑问，请参考源代码或提交 Issue。*
