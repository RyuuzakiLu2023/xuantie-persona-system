# 快速开始

当前为未发布的完整公开整备工作树。使用Python 3.10+，本机验证为3.11；安装依赖不止标准库，见pyproject.toml。

## 安装与向量

```bash
python -m venv .venv
# 激活虚拟环境后
python -m pip install .
ollama pull bge-m3
xtmem --help
```

Ollama需先安装并启动，默认127.0.0.1:11434；用XTMEM_EMBEDDING_URL改变地址。仅支持Ollama /api/embed，暂无OpenAI云端embedding接口。

## 固定身份与库

Linux/macOS：

```bash
export XTMEM_DB_PATH="$PWD/demo-memory.db"
export XTMEM_ACTOR=role-a
```

PowerShell：

```powershell
$env:XTMEM_DB_PATH = Join-Path (Get-Location) 'demo-memory.db'
$env:XTMEM_ACTOR = 'role-a'
```

没有设置时默认持久库在~/.xtmem/vector_store.db，不是内存数据库。解析不会将~当普通目录名。

生产解析优先级保持现役语义：显式参数、xtmem.yaml的memory_advanced、未被整段替换的宿主memory_advanced、xtmem.yaml顶层、XTMEM_DB_PATH、默认值。继承HERMES_HOME/MIMOX_HOME时请核对doctor，不要假定环境变量一定覆盖宿主配置。测试必须清除这些宿主变量。

## 两层记忆

```bash
xtmem store --agent role-a --namespace role-a --tier raw --text "对话原料：演示项目讨论数据库"
xtmem store --agent role-a --namespace role-a --tier refined --visibility shared --text "演示项目决定使用 SQLite"
xtmem recall --query "演示项目数据库" --cross --top 3
xtmem lookup --query "SQLite" --cross --top 3
```

role-a是写入身份，namespace是归属；通常必须一致。--cross只扩展查询域，不跳过private/shared-with检查。

精选层可以由人明确记录，也可以自动蒸馏产生。自动链路的窗口、模型和提交步骤见[蒸馏](distillation.md)，不是让使用者手工把所有流水改成refined。

## 归档与维护

```bash
# 先预览，不会执行删除
xtmem forget --id mem-xxxxxxxx
# 核对预览后才确认
xtmem forget --id mem-xxxxxxxx --confirm
xtmem doctor --help
xtmem reembed --help
```

forget是可追溯归档并防止WAL重放复活，不是把所有备份物理抹除。恢复需考虑库外状态，参见[已知问题](known-issues.md)。

## 多端

Hermes、DSH、ZCode/MCP接头均使用同一份安装引擎，身份/会话各自保留。完整安装步骤见[各端安装](clients.md)。不要把MemoryProvider文件直接当成一个独立服务器；它需要Hermes宿主接口。

## 验证

[公开验证记录](public-validation.md)列出本轮跑过与未跑的项目。核心合成测试和干净wheel安装分别核对；旧tests有私人部署布局假设，不因失败就删掉或称为跳过。
