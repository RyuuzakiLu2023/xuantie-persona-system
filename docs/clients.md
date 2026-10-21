# XTMEM client adapters

How the three public client adapters attach to the same installed engine, and exactly
what each one does (and does not do) automatically.

> **Version note.** The product version (`pyproject.toml`, currently `1.1.0.dev0`) and the
> adapter versions are **independent on purpose**. The ZCode plugin manifest, MCP server
> banner and DSH tool definitions describe the adapter surface; they only move when that
> surface changes. A product release does not automatically mean a new adapter version.
> If you need them equal, compare `pyproject.toml` `version` with
> `src/zcode/.zcode-plugin/plugin.json` `version` and the `SERVER_VERSION` constant in
> `src/zcode/mcp/xtmem_mcp.py`.

---

## Wheel 中的接头资源

源码用户直接使用 `src/zcode` 与 `src/dsh/xtmem-tools`。仅安装wheel的用户，可运行：

```bash
python -c "import sysconfig; print(sysconfig.get_path('data'))"
```

该目录下 `share/xtmem/zcode` 是可添加的本地插件市场，`share/xtmem/dsh/xtmem-tools` 是DSH插件目录。Hermes接头安装为 `xtmem_hermes` 包；源目录注册形式见第4节。使用这些资源的解释器须与安装XTMEM的环境一致，避免宿主PATH上的另一个python找不到引擎。

## 1. One engine, many endpoints

The **MCP server, the ZCode hooks and the DSH plugin** are thin: they never import the
engine and never open the memory database for writing. They shell out to the
**already installed** CLI:

```text
<python> -m xtmem <command> ...
```

`XTMEM_SCRIPT=/abs/path/xtmem.py` overrides the entry point for source-tree runs.
Never pass a `xtmem.exe` console-script path to Python — that file is a binary launcher.

**The Hermes provider is different**: it runs in-process with the host and imports the
`memory` engine package directly. It is the one adapter that is not a subprocess
client — see §4.

```text
   ZCode host  ──MCP stdio──► xtmem_mcp.py ──┐
   ZCode host  ──hooks─────► xtmem_hook.py ──┼──► python -m xtmem ──┐
   DSH host    ──cordis───► index.mjs ───────┘                     ├──► SQLite
   Hermes host ─provider─► provider.py ──(in-process)► memory ─────┘
```

The three CLI clients share the same identity gate, visibility rules, deduplication and
provenance code path — that is the point of routing them through the CLI. The Hermes
provider enters the engine in-process instead, so it uses the engine's own enforcement
(which is the same modules) rather than the CLI's argument-level checks.

### Configuration surface

| Variable | Meaning | Default |
|---|---|---|
| `XTMEM_ACTOR` | Identity for this endpoint | `host` (MCP), `zcode` (hooks), `dsh` (DSH); Hermes takes it from the host's `agent_identity` |
| `XTMEM_PYTHON` | Interpreter that runs the CLI | current interpreter |
| `XTMEM_SCRIPT` | Absolute path to `xtmem.py` | unset → `python -m xtmem` |
| `XTMEM_DB_PATH` | Explicit database file | engine default: `~/.xtmem/vector_store.db` (overridable with `XTMEM_HOME`; Hermes also reads `db_path` from its profile's `xtmem.yaml`) |
| `XTMEM_HOME` | Config/database directory | `~/.xtmem` |

`admin`, `owner` and `__admin__` are refused as actors by both the adapters and the
engine's serve protocol, so an adapter can never be started as an administrator.

### Explicit database beats host config

When `XTMEM_DB_PATH` is set, the adapters also **clear** `XTMEM_HERMES_HOME`,
`HERMES_HOME` and `MIMOX_HOME` from the child process. The CLI resolves its database as
`xtmem.yaml`/`config.yaml` (found via the home) → `XTMEM_DB_PATH` → default, so leaving a
home variable in place would let a host's YAML silently win over your explicit path and
send writes to a different database. The MCP server, the ZCode hook and the DSH plugin
all do this; `tests/test_public_clients.py` has a case per home variable that plants a
hostile `xtmem.yaml` pointing at `guard.db` and asserts it is never created.

A multi-endpoint machine sharing one database:

```bash
XTMEM_ACTOR=role-a XTMEM_DB_PATH=/srv/xtmem/vector_store.db
XTMEM_ACTOR=role-b XTMEM_DB_PATH=/srv/xtmem/vector_store.db
```

Each endpoint writes only its own namespace (plus `knowledge` with `shared`
visibility). Cross-domain reads require `cross=true` and still pass the visibility
filter.

---

## 2. ZCode — MCP server (primary path)

Install the CLI from the checkout first (this release is not on PyPI):
`python -m pip install .`. Then add `src/zcode/` as a local marketplace in ZCode and
install the `xtmem` plugin. The manifest starts `mcp/xtmem_mcp.py` with `XTMEM_ACTOR`
already set.

### Protocol

Newline-delimited JSON-RPC 2.0 over stdin/stdout. Implemented subset:
`initialize`, `notifications/*`, `ping`, `tools/list`, `tools/call`.
Protocol version is negotiated: a supported requested version is echoed, anything else
gets the newest supported one.

**stdout carries protocol frames only.** Every diagnostic goes to stderr. A single stray
log line on stdout breaks the host's line parser, so if a host reports "empty output",
run the server by hand and read stderr.

### Tools

| Tool | Arguments | Notes |
|---|---|---|
| `xtmem_recall` | `query`, `top` (≤200), `namespace`, `project`, `cross` | non-cross is restricted to the caller's namespace |
| `xtmem_store` | `text`, `project`, `importance` (0..1), `namespace`, `tags`, `tier`, `visibility` | writes own namespace; `knowledge` + `shared` is the only exception |
| `xtmem_lookup_cross` | `query`, `top` | keyword lookup across visible namespaces |
| `xtmem_lookup_id` | `id` | exact ID lookup, visibility still checked |
| `xtmem_msg_recent` | `top` | read-only mailbox view |
| `xtmem_msg_inbox` | `confirm=true` | marks messages read; irreversible |
| `xtmem_agents` | — | list registered actors |
| `xtmem_forget` | `id`, `confirm=true` | archives a memory; `confirm` is translated to the CLI's `--confirm` (without it the CLI only previews) |

`msg send` is deliberately **not** exposed: sending messages to other agents is an
authorized action and stays on the CLI.

### Serve fast path (optional)

`XTMEM_SERVE_MODE=auto` tries a resident loopback server before falling back to one
process per call:

- Transport failure (connection refused, timeout) → falls back to the CLI.
- **Authentication or protocol rejection is terminal — it never falls back**, so a wrong
  identity cannot silently look like a working connection.
- The server is spawned with an explicit credential, `XTMEM_ACTOR`, `XTMEM_DB_PATH` and
  its own PID file, so it cannot be hijacked onto a different database.

Default is `off`: the CLI path needs no port, no PID file and no credentials file.

### Automatic injection / Stop boundary

| Event | Behaviour |
|---|---|
| `SessionStart` | One read-only query for the newest `track=checkpoint` memory in the caller's namespace; injects a single resume line. No anchor → empty injection. |
| `UserPromptSubmit` | One `recall` (≤4 items, truncated by the route's token budget, each tagged `[m:<id>]`) plus a read-only unread-mailbox check. No cross-domain top-up here. |
| `Stop` | Scans the reply for `[m:mem-xxxxxxxx]` and marks each one; then stores one observation if the engine quality gate accepts it and the reply is ≥8 characters. |

The hook process only reads SQLite (a single SELECT); all writes go through the CLI.
Injected text is always passed through a brace neutraliser (consecutive `{{`/`}}` get a
zero-width space) because hosts run template interpolation over prompt sections.

---

## 3. DSH — cordis tool plugin

`src/dsh/xtmem-tools/` exports `name`, `inject` and `apply(ctx, config)`.

### Real installation steps

1. Install the SDK in the host's profile (it is a peer dependency, not bundled):
   ```bash
   npm install @deepseek-ai/dsh-tools
   ```
   The package must resolve from the profile that loads the plugin. On the reference
   machine it lives under `<DSH_HOME>/profiles/node_modules/@deepseek-ai/dsh-tools`
   (`@deepseek-ai/dsh-tools@0.2.1-rc.1` at the time of writing). Its peers
   (`@deepseek-ai/cordis`, `dsh-agent`, `dsh-llm`, `dsh-session`, `dsh-system-prompt`,
   …) must resolve too; installing the whole `@deepseek-ai/dsh` bundle gives you all of
   them.
2. Copy `xtmem-tools/` into the profile.
3. Insert the plugin into the profile's patch file:
   ```yaml
   - insert: { id: xtmem-tools, name: './xtmem-tools/index.mjs' }
   ```
4. Configure it (all keys optional):
   ```yaml
   xtmem-tools:
     python: python
     script: ''            # empty → `python -m xtmem`
     actor: dsh
     namespace: ''         # empty → same as actor
     dbPath: ''            # empty → engine default
     identityFile: ''      # empty → no identity blob injected
     memoryInject: 'on'    # 'off' disables the whole injection waterfall
     memoryNowTop: 5
     ownMinHits: 3
     autoStore: 'off'      # 'summary' enables turn-level raw logging
   ```

### What it registers

Three tools — `xtmem_recall`, `xtmem_store`, `xtmem_lookup_cross` — with the same
identity rules as the MCP server (identity from `actor`, never from arguments).

`memory-now.mjs` is a dependency-free module of pure functions used by the assembly
waterfall: `sanitizePromptText`, `idTagOf`, `exposedIdsIn`, `isSummaryBucket`,
`isRawTurnLog`, `pickOwnWindow`, `memoryNowText`, `memoryNowSectionText`. It can be
unit-tested with `node --test` without the host. `memoryNowText(data, label)` takes the
namespace label explicitly so multi-identity deployments do not render the wrong domain.

### Injection sections

`xtmem:identity` (only when `identityFile` is set), `xtmem:memory-now` (per-turn
recall), `xtmem:bulletin`, `xtmem:inbox`, `xtmem:memory` (fixed digest),
`xtmem:memory-usage` (rules), `xtmem:portrait`, `xtmem:time` (frozen per turn so the
prompt cache prefix stays stable).

### Boundaries

- The plugin is a **consumer** of the memory engine. It does not distill, prune or
  migrate; those are CLI jobs.
- `autoStore` writes raw turn logs (`tier=raw`). Those rows are excluded from the
  per-turn injection window and from the fixed digest, but remain searchable.
- `identityFile` is off by default: persona text belongs in the host's persona config,
  not in a memory adapter that would otherwise read a private file.

---

## 4. Hermes — MemoryProvider plugin

`src/hermes-plugin/` is packaged as `xtmem_hermes` (plus the top-level `memory` engine
package). It is the only adapter that runs **in-process** with the host, because Hermes
loads memory providers as Python objects rather than subprocesses.

### Installation

This release is **not on PyPI**. Install from the checkout or a locally built wheel;
`pip install xtmem` would pull an unrelated package by that name:

```bash
python -m pip install .            # from the repo root: memory + xtmem_hermes + `xtmem`
# or
python -m build --wheel && python -m pip install dist/*.whl
```

### How the host finds it

The plugin exposes `register(ctx)` in `__init__.py`, which calls
`ctx.register_memory_provider(XtmemMemoryProvider())`. `provider.py` also carries a
module-level `register()` with the same body, because the host loader supports both a
"register hook" path and a "MemoryProvider subclass" path — the docstring in
`src/hermes-plugin/__init__.py` records that convention, and the public whitepaper
(`docs/XTMEM-白皮书-技术产品导向-v1.md`) documents placing the plugin under the
profile's `plugins/memory/` and selecting it in `config.yaml`.

**This has not been exercised against a real Hermes host in this release.** The two
paths above are what the code provides; the exact directory layout, config key names and
loader behavior of your Hermes version must be confirmed against that host's own
documentation. What *is* verified here is that the module imports and registers
correctly under both load forms (see §5).

`__init__.py` imports `.provider` relatively and falls back to a same-directory
absolute import, so the directory form and the installed-package form both work.

### Configuration

Per-profile `$HERMES_HOME/xtmem.yaml`; see `xtmem.yaml.example`. Only the keys you set
are used. Notable ones:

| Key | Effect |
|---|---|
| `db_path` | Database file (relative to CWD, `~` expands) |
| ~~`namespace` / `project`~~ | **Not applied.** `provider.py` only reads `db_path`, `context_window`, `memory_advanced`, `prefetch_min_score`, `recall_cooldown_secs`, `resident_builder` and `stats`. Namespace comes from the host's `agent_identity`/`profile`; project from `agent_workspace`. |
| `prefetch_min_score` | Quality gate for injection (default `0.01` = legacy) |
| `prefetch_wait_secs` | Max time a turn may block on prefetch (must stay under the host's 8s deadline) |
| `read_tools` | Expose the read-only `xtmem_recall` / `xtmem_lookup` tools |
| `stats.*` | Activity hooks (`record_events`, `record_utility`, `store_quality_gate`) |
| `recall_policy.*` | Keyword lists and thresholds for the retrieval gate |

### Host dependency

`provider.py` needs exactly two symbols from the host:
`agent.memory_provider.MemoryProvider` and `RecallStatus`. Nothing else. That is why a
nine-line stub module is enough to import and unit-test the provider outside Hermes.

---

## 5. Verified vs not verified

Verified on a synthetic database with a scratch virtualenv (no real host, no production
database):

| Item | Status |
|---|---|
| `pip install` wheel → `python -m xtmem` works | ✅ verified |
| `xtmem_hermes` imports from the installed wheel (relative import) | ✅ verified |
| Same plugin loaded by file path (absolute-import fallback) | ✅ verified |
| MCP `initialize` / `ping` / `tools/list` (8 tools) | ✅ verified |
| MCP `store` → `recall` → `lookup_id` round trip | ✅ verified |
| MCP cross-namespace write/read denial | ✅ verified |
| MCP `forget` requires `confirm`, and `confirm=true` removes exactly one row | ✅ verified |
| MCP survives malformed byte lines without polluting stdout | ✅ verified |
| MCP serve fast path (`XTMEM_SERVE_MODE=auto`) end-to-end | ✅ verified by hand |
| Explicit `XTMEM_DB_PATH` wins over a hostile `HERMES_HOME` / `MIMOX_HOME` / `XTMEM_HERMES_HOME` | ✅ verified (MCP + hook) |
| Hooks: SessionStart / UserPromptSubmit / Stop output shape | ✅ verified |
| Visibility isolation between `role-a` and `role-b` (private) | ✅ verified |
| DSH plugin loads under a stubbed `@deepseek-ai/dsh-tools`, registers 3 tools, assembles sections | ✅ verified with stub only |
| DSH plugin inside a real DeepSeek Harness instance | ❌ **not verified** |
| ZCode host actually launching the MCP server and hooks | ❌ **not verified** |
| Hermes host loading the provider and running a turn | ❌ **not verified** |
| Ollama semantic retrieval (needs `:11434` + `bge-m3`) | ❌ not exercised; keyword path used |
| Windows GBK-configured console output | ⚠️ stdout is forced to UTF-8 bytes; not tested against a GBK host |

Reusable test script: `tests/test_public_clients.py` (MCP protocol, permissions,
`confirm` paths, hook shapes). It creates its own temporary database per test and never
touches `XTMEM_DB_PATH` of the caller's shell.
