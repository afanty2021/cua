# cua-bench 模块文档

[根目录](../../CLAUDE.md) > [libs](../) > **cua-bench**

> **最后更新**: 2026-05-01 | **版本**: 0.1.x | **状态**: 活跃开发中**

---

## 模块职责

`cua-bench` 是 **跨平台计算机使用代理基准测试框架**，提供：

1. **标准化环境**: 可验证的跨平台测试环境
2. **任务定义**: 声明式任务描述语言
3. **数据集管理**: 预构建任务集合（cua-bench-basic、cua-bench-workflows）
4. **评估指标**: 成功率、步骤数、时间、成本
5. **可视化**: 轨迹回放、热力图、对比图表

---

## 入口与启动

### 安装

```bash
# 从源码安装
cd libs/cua-bench
pip install -e .

# 安装开发依赖
pip install -e ".[dev,browser,server,rl]"
```

### 基本使用

```bash
# 列出可用任务
cua-bench list

# 运行单个任务
cua-bench run click-button

# 运行完整基准测试
cua-bench run --dataset cua-bench-basic

# 交互式运行
cua-bench interact --task click-button
```

---

## 对外接口

### CLI 命令

#### 任务管理

```bash
# 列出所有任务
cua-bench list

# 查看任务详情
cua-bench info click-button

# 创建新任务
cua-bench create my-task --template basic

# 验证任务配置
cua-bench validate my-task
```

#### 运行测试

```bash
# 运行单个任务
cua-bench run click-button

# 运行数据集
cua-bench run --dataset cua-bench-basic

# 并行运行
cua-bench run --workers 4

# 指定平台
cua-bench run --platform linux
```

#### 结果管理

```bash
# 查看结果
cua-bench results

# 导出结果
cua-bench export --format json --output results.json

# 生成报告
cua-bench report --format html

# 对比两次运行
cua-bench compare run1 run2
```

### Python API

```python
from cua_bench import Task, Runner, Evaluator

# 定义任务
task = Task(
    name="click-button",
    description="点击页面上的按钮",
    platform="linux",
    setup_commands=[
        "python -m http.server 8000"
    ],
    eval_steps=[
        {
            "action": "screenshot",
            "verify": "button_clicked.png"
        }
    ]
)

# 运行任务
runner = Runner(agent=agent)
result = runner.run(task)

# 评估结果
evaluator = Evaluator()
score = evaluator.evaluate(result, task)
print(f"得分: {score}")
```

---

## 关键依赖与配置

### 依赖项

```toml
[dependencies]
# Web 框架
fastapi>=0.100.0
uvicorn>=0.23.0

# 浏览器自动化
playwright>=1.40.0

# 数据处理
pandas>=2.0.0
numpy>=1.24.0

# 图像处理
pillow>=10.0.0

# 异步支持
aiohttp>=3.9.0
websockets>=12.0
```

### 配置文件

```yaml
# cua-bench.yaml
benchmarks:
  - name: click-button
    dataset: cua-bench-basic
    platform: linux
    timeout: 60

  - name: drag-slider
    dataset: cua-bench-basic
    platform: windows
    timeout: 120

agents:
  - name: claude-sonnet-4-5
    model: anthropic/claude-sonnet-4-5
    api_key: ${ANTHROPIC_API_KEY}

output:
  format: json
  path: ./results
  include_screenshots: true
```

---

## 数据模型

### 任务定义

```python
from pydantic import BaseModel

class Task(BaseModel):
    name: str
    description: str
    platform: str  # "linux", "macos", "windows", "android"

    setup_commands: list[str]
    eval_steps: list[EvaluationStep]

    metadata: dict = {}
    tags: list[str] = []
```

### 评估结果

```python
class EvaluationResult(BaseModel):
    task_name: str
    agent_name: str

    success: bool
    steps_taken: int
    time_elapsed: float  # 秒
    cost: float  # USD

    screenshots: list[str]
    error_message: str | None = None

    metadata: dict = {}
```

---

## 测试与质量

### 测试结构

```
libs/cua-bench/tests/
├── test_gym_interface.py      # 核心 API 测试
├── test_worker_client.py       # Worker 客户端测试
├── test_worker_server.py       # Worker 服务器测试
├── test_run_benchmark.py       # 基准测试运行器
├── test_worker_manager.py      # Worker 管理器
└── test_actions.py             # 动作解析测试
```

### 运行测试

```bash
cd libs/cua-bench

# 运行所有测试
uv run --with pytest pytest cua_bench/tests/ -v

# 运行特定测试
pytest tests/test_gym_interface.py -v

# 带覆盖率
pytest tests/ --cov=cua_bench --cov-report=term-missing

# 基准测试基础设施
uv run python -m cua_bench.scripts.benchmark_workers --num_workers 16
```

### 测试策略

| 测试类型 | 范围 | 方法 |
|---------|------|------|
| **单元测试** | 单个函数 | Mock 依赖 |
| **集成测试** | 模块间 | Playwright 模拟环境 |
| **E2E 测试** | 完整流程 | 真实沙箱 |
| **性能测试** | 基础设施 | 并发 Worker |

---

## 常见问题 (FAQ)

### Q1: 如何创建自定义任务？

```bash
# 使用模板创建
cua-bench create my-task --template basic

# 编辑任务配置
cd my-task
vim task.yaml

# 验证任务
cua-bench validate
```

### Q2: 如何运行特定平台测试？

```bash
# 仅 Linux
cua-bench run --platform linux

# 仅 Windows
cua-bench run --platform windows

# 多平台
cua-bench run --platform linux,macos
```

### Q3: 如何分析失败的任务？

```bash
# 查看失败任务的日志
cua-bench results --filter failed

# 查看详细轨迹
cua-bench trace <run-id>

# 导出失败案例用于调试
cua-bench export --filter failed --output failed.json
```

---

## 数据集

### cua-bench-basic

基础 GUI 交互任务集合：

| 任务 | 描述 | 平台 | 难度 |
|------|------|------|------|
| **click-button** | 点击按钮 | 跨平台 | ⭐ 简单 |
| **click-icon** | 点击图标 | 跨平台 | ⭐ 简单 |
| **color-picker** | 颜色选择器 | 跨平台 | ⭐⭐ 中等 |
| **date-picker** | 日期选择器 | 跨平台 | ⭐⭐ 中等 |
| **drag-drop** | 拖放操作 | 跨平台 | ⭐⭐ 中等 |
| **drag-slider** | 拖动滑块 | 跨平台 | ⭐⭐ 中等 |
| **fill-form** | 填写表单 | 跨平台 | ⭐⭐⭐ 困难 |
| **right-click-menu** | 右键菜单 | 跨平台 | ⭐⭐ 中等 |
| **select-dropdown** | 下拉选择 | 跨平台 | ⭐⭐ 中等 |
| **spreadsheet-cell** | 电子表格 | 跨平台 | ⭐⭐⭐ 困难 |
| **toggle-switch** | 切换开关 | 跨平台 | ⭐ 简单 |
| **typing-input** | 输入文本 | 跨平台 | ⭐ 简单 |
| **video-player** | 视频播放器 | 跨平台 | ⭐⭐⭐ 困难 |

### cua-bench-workflows

复杂工作流任务集合：

| 任务 | 描述 | 步骤数 |
|------|------|--------|
| **minesweeper** | 扫雷游戏 | 10-20 |
| **notepad** | 记事本编辑 | 5-10 |
| **file-manager** | 文件管理 | 8-15 |

---

## 相关文件清单

### 核心文件

- `cua_bench/__init__.py` - 模块入口
- `cua_bench/core.py` - 核心类
- `cua_bench/environment.py` - 环境管理
- `cua_bench/runner/task_runner.py` - 任务运行器

### CLI 文件

- `cua_bench/cli/main.py` - CLI 入口
- `cua_bench/cli/commands/` - 命令实现

### 数据集文件

- `datasets/cua-bench-basic/` - 基础任务
- `datasets/cua-bench-workflows/` - 工作流任务
- `tasks/winarena_adapter/` - WinArena 适配器

### 文档文件

- `README.md` - 主文档
- `plan.md` - 开发计划
- `tasks/winarena_adapter/README.md` - WinArena 文档

---

## 变更记录 (Changelog)

### 当前版本
- 添加 Android 任务支持
- 改进评估指标
- 优化并行执行性能

### 历史版本
- 0.1.0: 初始版本
- 添加 WinArena 适配器
- 添加 Web UI

---

*本文档由 AI 自动生成，反映模块当前状态。如有疑问，请参考源代码或提交 Issue。*
