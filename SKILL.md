---
name: magpie
description: 使用 magpie-agent 无交互 CLI 查询 Magpie 管理的 Agent、模型、供应商、方案和用量，预览并执行模型或推理强度变更。用户提到 Magpie 管理、Magpie CLI 或查询其配置时使用；不用于直接向模型发起推理请求。
metadata:
  tested-cli-version: "0.2.0"
---

# Magpie Agent CLI

使用 `magpie-agent`，它只调用 [Magpie 官方 CLI](https://usemagpie.ai/docs/zh/start#cli)。默认 stdout 为单个 JSON envelope，业务命令不提示、不自动打开浏览器、不自动升级，不使用 MCP 或内部 HTTP。

## 安装与发现

```powershell
magpie-agent version
magpie-agent schema
magpie-agent doctor
```

要求 Python 3.12+ 和已安装的官方 Magpie。入口不在 PATH 时，可用 `python -m magpie_agent.cli` 替代 `magpie-agent`。在本 Skill 根目录安装：

```powershell
python -m pip install --no-deps ./cli
```

底层复用官方已安装程序及其登录。`MAGPIE_AGENT_EXE` 可指定可信官方可执行文件的绝对路径，优先于 PATH；不要传 API Key。`doctor` 只证明安装和帮助发现，不能证明认证有效、网关健康或模型推理成功。

CLI 源码、测试和安装包统一位于此 Skill 的 `cli/` 目录；任务记录不作为运行或安装依赖。

## 常用查询

```powershell
magpie-agent agents list --limit 30
magpie-agent agents get --agent codex
magpie-agent models list --limit 20 --fields text
magpie-agent providers list --limit 20
magpie-agent presets list --limit 20
magpie-agent profiles list --limit 20
magpie-agent accounts status
magpie-agent usage get --period 7d --limit 50
```

读取 `data.items`：这是脱敏后的官方文本行，稳定字段为 `line`、`text`，不假装是结构化模型 API。`meta.has_more` 为 true 时按 `meta.next_action.argv` 继续读取；分页每次重取，不保证活动数据的快照一致性。

`providers list` 仅返回供应商显示名称，隐藏账号、地址与密钥列。用量区分网关记录与 `local_sessions`，费用是上游估价，不是账单实扣。模型目录可见不等于实际推理已验证。

公共 flags 放在具体动作后：默认总超时 90 秒、最大 300；默认 limit=100、最大 500。`--output jsonl --quiet` 返回单行终态。`--input -` 接收一个 JSON 对象（最大 64 KiB），同字段不得由 flag 和 JSON 重复提供。

## 变更流程

先通过目录和 schema 确认目标。写操作默认预览：

```powershell
magpie-agent agents set-effort --agent codex --effort high --dry-run
```

用户已授权该变更时，检查 `data.plan.inputs`、`before`、`noop`、`expires_at`，再以相同参数去掉 `--dry-run` 并添加 `--confirm HASH`，HASH 取刚返回的 `data.plan.plan_hash`。不重复询问已经明确授权的事项；没有业务授权则保留预览。

摘要绑定输入、当前状态、后端二进制和用户，最多有效到下一个 5 分钟边界。`CONFLICT` 时重新读取与预览。不要手工构造 argv 或把 shell 文本当执行计划。

检查 `state` 与 `verified`。模型、effort、discovery 有字段回读验证；`profiles apply` 与 `agents reset` 仅执行并回读，明确返回 `verified=false`，不得声称已完整验证方案或恢复值。模型配置从新会话验证，不能说已有会话已经切换。

其他操作与 stdin 示例见 [references/operations.md](references/operations.md)。

## 错误恢复

检查退出码、`ok` 和 `error.code`，不要靠英文 message 判断：

| 退出码 | 决策 |
|---|---|
| 2 | 参数错误，查 schema 或动作 --help |
| 3 | 缺依赖、认证或能力；先解决状态，不自动升级 |
| 4 | 读超时/服务故障按 retryable 做有界重试；输出超限停止 |
| 5 | 配置、摘要或写锁冲突，核对并重新预览 |
| 1 | 未知上游/内部失败，停止盲目重试 |
| 130 | 用户取消，停止 |

任何写入返回 `outcome=unknown` 都先读取当前状态，禁止盲目重复提交。写操作不自动重试。审计在 `~/.local/state/magpie-agent/audit.jsonl`，无自动上传。写锁仅协调本封装，不能阻止桌面客户端或其他程序并发改配置。

## 已验证边界

封装 0.2.0、官方 Magpie 0.1.1296，Windows 实测。`tools list`、`logs get` 当前返回 `UNSUPPORTED_CAPABILITY`。密钥添加、交互登录、轮换账号、生成图片、启动器和无限日志跟随不在本版本执行面，原因见 `schema.excluded_capabilities`。其他操作系统未经验证。

原 `scripts/probe.py` 保留为旧版只读探测脚本；新工作优先使用上述安装入口和统一协议。
