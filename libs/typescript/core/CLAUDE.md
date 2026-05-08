# @trycua/core 模块文档

[根目录](../../../CLAUDE.md) > [libs/typescript](../) > **core**

> **最后更新**: 2026-05-01 | **版本**: 0.1.x | **状态**: 稳定**

---

## 模块职责

`@trycua/core` 是 Cua TypeScript 生态的**核心工具库**，提供：

1. **遥测系统**: PostHog 集成的匿名使用统计
2. **日志记录**: 结构化日志输出
3. **类型定义**: 通用 TypeScript 类型
4. **工具函数**: 跨包共享的实用函数

---

## 入口与启动

### 安装

```bash
# 作为依赖安装
npm install @trycua/core

# 作为开发依赖安装
npm install -D @trycua/core
```

### 基本使用

```typescript
import { telemetry, logger } from '@trycua/core';

// 启用遥测
telemetry.enable();

// 记录事件
telemetry.track('sandbox_created', {
  os: 'linux',
  runtime: 'docker'
});

// 日志输出
logger.info('Sandbox created successfully');
logger.error('Failed to create sandbox', { error: err });
```

---

## 对外接口

### 遥测 (Telemetry)

```typescript
import { telemetry } from '@trycua/core';

// 配置遥测
telemetry.configure({
  apiKey: 'phc_xxx',
  enabled: true,
  debug: false
});

// 记录事件
telemetry.track('event_name', {
  property1: 'value1',
  property2: 'value2'
});

// 页面视图
telemetry.pageView('sandbox_page');

// 用户识别
telemetry.identify('user_id', {
  email: 'user@example.com',
  plan: 'pro'
});

// 禁用遥测
telemetry.disable();

// 检查状态
const isEnabled = telemetry.isEnabled();
```

### 日志 (Logger)

```typescript
import { logger } from '@trycua/core';

// 配置日志
logger.configure({
  level: 'debug',  // 'debug' | 'info' | 'warn' | 'error'
  pretty: true     // 美化输出
});

// 日志级别
logger.debug('Debug message', { data: {} });
logger.info('Info message');
logger.warn('Warning message');
logger.error('Error message', { error: new Error() });

// 创建子日志器
const childLogger = logger.child({ component: 'sandbox' });
childLogger.info('Component initialized');
```

### 类型定义

```typescript
import type {
  SandboxConfig,
  ImageConfig,
  RuntimeConfig
} from '@trycua/core';

interface SandboxConfig {
  image: ImageConfig;
  runtime: RuntimeConfig;
  resources?: {
    memory?: string;
    cpus?: number;
  };
}
```

---

## 关键依赖与配置

### 依赖项

```json
{
  "dependencies": {
    "pino": "^9.7.0",
    "uuid": "^11.0.0"
  },
  "devDependencies": {
    "@types/node": "^22.15.17",
    "@types/uuid": "^10.0.0",
    "typescript": "^5.8.3"
  }
}
```

### 配置示例

```typescript
import { telemetry, logger } from '@trycua/core';

// 环境变量配置
const config = {
  telemetry: {
    apiKey: process.env.POSTHOG_API_KEY,
    enabled: process.env.TELEMETRY_ENABLED !== 'false',
    debug: process.env.NODE_ENV === 'development'
  },
  logging: {
    level: process.env.LOG_LEVEL || 'info',
    pretty: process.env.NODE_ENV === 'development'
  }
};

telemetry.configure(config.telemetry);
logger.configure(config.logging);
```

---

## 测试与质量

### 测试结构

```
libs/typescript/core/tests/
├── telemetry.test.ts
├── logger.test.ts
└── utils.test.ts
```

### 运行测试

```bash
# 运行所有测试
pnpm test

# 运行特定测试
pnpm test telemetry.test.ts

# 带覆盖率
pnpm test --coverage
```

---

## 常见问题 (FAQ)

### Q1: 如何禁用遥测？

```typescript
import { telemetry } from '@trycua/core';

// 方法 1: 代码中禁用
telemetry.disable();

// 方法 2: 环境变量
// TELEMETRY_ENABLED=false
```

### Q2: 如何在生产环境使用？

```typescript
import { telemetry, logger } from '@trycua/core';

// 生产环境配置
telemetry.configure({
  apiKey: process.env.POSTHOG_API_KEY!,
  enabled: true,
  debug: false
});

logger.configure({
  level: 'info',  // 降低日志级别
  pretty: false   // 禁用美化输出
});
```

---

## 相关文件清单

- `src/index.ts` - 模块入口
- `src/telemetry.ts` - 遥测系统
- `src/logger.ts` - 日志系统
- `src/types.ts` - 类型定义
- `src/utils.ts` - 工具函数

---

## 变更记录 (Changelog)

### 当前版本
- 初始稳定版本
- PostHog 遥测集成
- Pino 日志系统

---

*本文档由 AI 自动生成，反映模块当前状态。*
