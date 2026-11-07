# Magpie Agent Skill

面向 AI Agent 的 Magpie CLI 适配器与配套 Skill。通过官方 `magpie` 查询 Agent 配置、模型、供应商、方案和用量，并为配置变更提供预览、确认摘要、审计及回读检查。

这是独立维护的社区项目，业务接口依据 [Magpie 官方 CLI 文档](https://usemagpie.ai/docs/zh/start#cli)，不包含 Magpie 程序本身。

## 安装与开始

要求 Python 3.12+，并已安装官方 Magpie。当前验证环境为 Windows、Python 3.12、Magpie 0.1.1296；其他平台尚未验证。

```powershell
git clone https://github.com/runningZ1/magpie-agent-skill.git magpie
cd magpie
python -m pip install --no-deps ./cli
magpie-agent version
magpie-agent schema
magpie-agent doctor
```

安装入口不在 PATH 时，可用 `python -m magpie_agent.cli` 替代 `magpie-agent`。运行时只依赖 Python 标准库；源码安装由 pip/setuptools 构建，构建环境可能需要下载 setuptools。

作为 Skill 使用时，将整个目录放入宿主支持的技能目录，保持名称 `magpie`，加载 [SKILL.md](SKILL.md)。源码、测试和构建配置均在本仓库内。

## 常用命令

```powershell
magpie-agent agents list --limit 20
magpie-agent agents get --agent codex
magpie-agent models list --limit 20 --fields text
magpie-agent providers list
magpie-agent profiles list
magpie-agent accounts status
magpie-agent usage get --period 7d --limit 50
```

默认 stdout 为单个 JSON envelope，成功结果位于 `data`，失败位于 `error`。`schema` 列出命令、字段、范围和执行限制；所有参数放在具体动作后，动作帮助使用 `--help`。

| 接口 | 行为 |
|---|---|
| `version / schema / errors` | 本地发现，不要求连接官方后端 |
| `doctor` | 核对官方程序与帮助声明，不代表认证或推理成功 |
| 查询命令 | 默认 100 行、最大 500；支持 offset、字段投影和 JSONL |
| 写操作 | 默认预览，显式 `--confirm HASH` 提交；不自动重试 |
| 执行 | 固定 argv、关闭上游 stdin、总超时和输出上限 |
| 供应商输出 | 只公开显示名称，抑制密钥与账号列 |

上游主要返回人类文本，本适配器稳定封装为 `data.items` 中的 `line/text`。分页每次重新查询，不保证活动数据的快照一致性。用量可能同时包含网关记录与本地会话估价；模型目录可见不证明模型推理可用。

## 配置变更

```powershell
magpie-agent agents set-effort --agent codex --effort high --dry-run
```

检查返回的 `data.plan`。业务变更已获用户授权时，以相同参数去掉 `--dry-run`，添加 `--confirm`，值使用当前 `plan_hash`。摘要绑定输入、当前状态、后端程序和有效时间；冲突或到期须重新预览。

模型、推理强度、discovery 有精确字段回读；应用方案与恢复默认只回读状态，明确返回 `verified=false`。`outcome=unknown` 时先查询现场，禁止盲目重发。详见 [操作参考](references/operations.md)。

## 测试与打包

```powershell
cd cli
python -m unittest discover -s tests -v
python -m pip wheel --no-deps . --wheel-dir dist
```

契约测试使用独立假后端，覆盖解析、输出、脱敏、超时、参数注入、计划校验和写入失败。33 项测试基线通过；不把假后端写入等同于真实配置变更验证。

真实只读与预览验收需先检查 [验收脚本](cli/tests/live_acceptance.py) 中的后端版本和模型示例，再显式运行：

```powershell
python tests/live_acceptance.py --report ./live-acceptance.json
python tests/installed_smoke.py --report ./installed-smoke.json
```

`installed_smoke.py` 需要先构建对应 wheel。报告被 Git 忽略，不应提交本机账号、路径或会话数据。`scripts/probe.py` 保留为旧版只读探测入口。

## 边界与维护

- 底层 0.1.1296 缺少 `tools`、`log`；对应命令返回 `UNSUPPORTED_CAPABILITY`。
- 本版不封装密钥添加、交互登录、轮换账号、生图、启动器和无限日志跟随；原因可在 `schema.excluded_capabilities` 查询。
- 本地写锁仅协调本适配器，不能阻止官方客户端并发修改。没有官方事务或 exactly-once 保证。
- `MAGPIE_AGENT_EXE` 可指定可信官方程序绝对路径，优先于 PATH。封装使用主机用户权限，不提供 OS 沙箱。
- 审计位于 `~/.local/state/magpie-agent/audit.jsonl`；不自动上传、不自行更新 Magpie。

更新从源码或 wheel 显式安装；卸载适配器使用 `python -m pip uninstall magpie-agent-cli`，不删除官方程序、登录或配置。

## 开源协议

本项目采用 [MIT License](LICENSE)。Magpie 的名称、程序及其自身协议归原项目所有；本仓库不包含其二进制，也不代表官方发布。
