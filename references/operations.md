# 配置操作

来源：https://usemagpie.ai/docs/zh/start#cli。下列为 magpie-agent 0.2.0 的类型化入口，不直接编辑配置。

| 目标 | 预览命令 | 官方映射 |
|---|---|---|
| 设置模型 | `magpie-agent agents set-model --agent codex --model codex/gpt-6.1-sol --dry-run` | `magpie codex codex/gpt-6.1-sol` |
| 推理强度 | `magpie-agent agents set-effort --agent codex --effort high --dry-run` | `magpie codex effort high` |
| 移除接入 | `magpie-agent agents reset --agent codex --dry-run` | `magpie codex default` |
| 保存方案 | `magpie-agent profiles save --name work --dry-run` | `magpie save work` |
| 应用方案 | `magpie-agent profiles apply --name work --dry-run` | `magpie use work` |
| 关闭登录发现 | `magpie-agent accounts discovery --enabled false --dry-run` | `magpie accounts discovery off` |

模型和方案必须来自当前查询，示例值不保证永远存在。已有用户业务授权时，将 --dry-run 替换成 --confirm HASH，使用当前预览返回的 plan_hash。不允许换目标或直接传执行 argv。

effort 限 low/medium/high/xhigh；字段必须由 Agent 专用帮助声明。上游对具体值有其他限制时失败，不擅自换值。模型先检查当前目录。发现参数或输出格式不兼容时停止，不能静默猜测。

profiles save 可能覆盖同名方案，预览时检查现有方案和当前配置。apply 影响多个 Agent，reset 移除接入，都不是诊断步骤。save 验证名称出现；apply 和 reset 仅有执行与回读证据，返回 state=dispatched_read_back、verified=false，需业务侧进一步核对。

## JSON 输入

```powershell
'{"agent":"codex","effort":"high"}' | magpie-agent agents set-effort --input - --dry-run
```

可以用文件路径替代 -。官方子进程 stdin 始终关闭。对象只接受 schema 声明的业务字段，拒绝 shell、任意 argv 和未知键。

## 能力缺失

```powershell
magpie-agent tools list
magpie-agent logs get --lines 20
```

当前后端 0.1.1296 未声明这两项，预期退出 3、UNSUPPORTED_CAPABILITY。不能用 MCP 的数据冒充 CLI 结果，不自动升级后端。

## 安装与回收

version 显示封装版本，doctor 显示官方程序路径、版本与 SHA256。更新须显式安装本地 wheel，不自行联网更新。卸载封装使用 `python -m pip uninstall magpie-agent-cli`；不卸载官方 Magpie，不清除其登录与配置。审计单独保留。
