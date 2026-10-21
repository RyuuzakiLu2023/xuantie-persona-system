# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/test_xtmem_d31_noise_r45.py · D3-1 扩判据守卫（R4 DOM残渣 / R5 进程表）
#
#   ▍这是什么
#   maintainer派单《D3-1 噪声过滤器扩判据》（2026-09-12）§二守卫：
#   用例 fixture 直接取用户A判卷五对原文（XTMEM-P7.1-13条未处置-用户A判卷版-20260912.md），
#   4 垃圾样本必须被 is_noise_text 命中并报对判据类（R4/R5），2 正经样本必须放行。
#
#   ▍修改铁律
#   1. 读取路径按绝对路径直载运行时 noise_filter.py（不插 sys.path，避免污染 pytest 会话解析，
#      同 P7.2 守卫口径）；反向验证 sabotage 该文件必红。
#   2. 断言只用纯文本 fixture，严禁连生产库（本单零生产写）。
#   3. ⚠️ 2478-B（裸 JSON 堆）本轮【未覆盖】——两层判据都够不着，已如实记入回执发现项，
#      本文件只断言已验证覆盖的样本，不做假绿。
#
#   ▍更新记录
#   *更新：2026-09-12 · 创建（D3-1 扩判据守卫，fixture 取自判卷原文）*
# ================================================================
import importlib.util

from pathlib import Path
_REPO = Path(__file__).resolve().parents[1]
MASTER_NOISE = str(_REPO / "src" / "hermes-plugin" / "memory" / "noise_filter.py")
_spec = importlib.util.spec_from_file_location("xtmem_noise_under_test", MASTER_NOISE)
NF = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(NF)

# 探针/断言统一走 enabled=True 的判据配置（判据本身与 enabled 开关解耦）
CFG = dict(NF.DEFAULT_CFG)
CFG["enabled"] = True

# ── fixture（判卷原文，快照 mem-id 抓取，零手抄）────────────────────────────
J2369A = 'DSH 自动记忆（turn 131, session session-<SESSION_ID>）\n用户: 不是，从头到尾审一遍\n本鱼: 进项判关闭 ④ 预期项防误判</span></div>\n<div class="row"><span class="lab">全貌表</span> <span class="path">R1→P5.1 逐 Phase：交付 / 审计锚点 / 状态</span></div>\n<div class="row"><span class="lab">数据基线</span> <span class="path">74 passed · 镜像无 DIFF · 9900/9901=200 · 20000 ok · cron ~26 条</span></div>\n</div>\n<div class="foot">——assistant-c,深海里调度的鱼</div>\n</div>\n\n**这版和 v7 的区别**：v7 只审 P5.1 新批；v8 是**整条时间线的总账**——\n\n- **① 未审交付全量实跑**：A2A-4 / 库层 / 后端+常驻 / 调度器 / 守门端 / 尾巴，逐条验收点；\n- **② 历史已审锚点回归**：BF-1 自环 / BF-3 fail-closed / BF-7 阈值 / 发现30 守卫 / 分层路由 / 实体地基 / P5.0 波2·3 / A2A-1·2 探活 / spec 版本四处对齐——**只抽锚点复核，不重跑全量**；\n- **③ 挂账跟进项判定**：⑫ 画像污染 / ⑯ FTS / ⑰ sign 记账 / ⑳ :322 lookup / ㉑ dsh-tools 分叉 / ㉚ 98 条——逐条要求 maintainer 填「关闭/仍挂」；\n- **④ 预期项防误判**：影子期 / A2A-3 搁置 / 前端用户A / 二期-C / Lint·折叠·hanlp——判缺失=误报，红线写死。\n\n已记库 mem-a291ceb3。要审时直接把 v8 丢给 maintainer，她拿到就能从头到尾开审。'
J2369B = 'DSH 自动记忆（turn 119, session session-<SESSION_ID>）\n用户: 那个审计是不是可以一起做？\n本鱼: v class="line"></div>\n<div class="cab">\n<div class="row"><span class="ok">📄</span> <span class="path"><XT_ROOT>\\new\\XTMEM-审计提示词-v6-P51.md</span></div>\n<div class="row"><span class="lab">审什么</span> <span class="path">A2A-1 assistant-a（已交付）+ A2A-2 assistant-c（待写）</span></div>\n<div class="row"><span class="lab">基准</span> <span class="path">方案 v2.4 §二/§四/§五 + A2A-2 实施文档 v1</span></div>\n<div class="row"><span class="lab">防误判</span> <span class="path">A2A-4/-3、库存层/后端/前端 标为本批不做</span></div>\n</div>\n<div class="foot">——assistant-c,深海里调度的鱼</div>\n</div>\n\n**为啥现在就够审**：v6 把验收点按「已交付」和「待交付」分开了——A2A-1 的配置/9900 探活/card/绑定/回滚都写死成可实测项；A2A-2 则对照实施文档验端点/协议/安全/路由/审计，并把「接流取回复（方案 A）不确定」标成**诚实标注不判负**。这样新会话一写完插件，maintainer拿 v6 直接审，不用再补上下文。\n\n已把两份文档都索引进公共库（mem-9732f412 / mem-cc82c611）。你切到创造模式开新会话，让它写 `a2a-platform.mjs`；写完在本会话（或直接）把 v6 甩给 maintainer 开审就行。'
J2644A = 'DSH 自动记忆（turn 188, session session-<SESSION_ID>）\n用户: 现在可以重启了吗\n本鱼: 可以重启，但**先对一下"重启"指的是哪几样**——别把不该动的动了：\n\n**当前进程实况：**\n\n| 进程 | 端口 | 状态 | 重启理由 |\n|---|---|---|---|\n| gateway 41036（assistant-a宿主） | 9900 | 🟢 活着 | **必须重启**（BF-2 红线：本批改了 provider.py + 引擎连接等 plugins/**.py，不重启=旧字节码跑新期待）——**会打断assistant-a在线会话几秒** |\n| api_server | 20000 | 🔴 已死 | 直接重拉即可（带 `XTMEM_REST_ALLOW_LOOPBACK_NOAUTH=1`），不是"重启" |\n| DSH web 28000 | 28000 | 🟢 活着 | 重启它会断**我当前会话几秒**，但 A2A 会话锚 + memory-now 才生效 |\n\n**我的建议（按优先级）：**\n> **① 先重启 gateway 41036**（这是 BF-2 硬红线，最该做）→ **② 重拉 api_server** → 这两样做完，生产侧新代码全链生效。\n> DSH web 28000 的 A2A 锚 + memory-now 是 dsh 侧体验优化，不急，可以等你有空时再单独重启（届时我会自断几秒，不影响assistant-a/zcode）。\n\n**assistant-a侧的注意**：gateway 41036 一重启，她当前正在跑的对话会中断——如果你正跟她聊，建议先说一声或等她闲时再重启。\n\n要我**现在只重启 gateway + 重拉 api_server**，还是**三个全来**？你点头我就动手，动完逐项验活 + 报告。'
J2478B = 'DSH 自动记忆（turn 29, session session-<SESSION_ID>）\n用户: {\\n  "stable": "object",\\n  "ownedStyleCount": 0,\\n  "cardElement": null,\\n  "totalStyles": 137,\\n  "styleHeads": [\\n    "dsh-raw-html:\\\\n/* 需求 16：手机端设置页改造。属性由 settings-mobile.t",\\n    "dsh-raw-html:\\\\n@media (pointer: coarse) {\\\\n  [data-slot",\\n    "@deepseek-ai/dsh-client-ui-directory-picker-browse:.ZuhsRW_dialog.ZuhsRW_dialog{--dsh-scrol",\\n    "none:@font-face{font-family:\'Lanxi-WenKai\';sr"\\n  ],\\n  "renderToggle": "未找到"\\n}`\n本鱼: er`。本鱼直接在浏览器里用 `render()` 这个函数，把**你那条卡片的原始 HTML 喂进去**，看它返回什么——如果返回 null 或空，就是引擎 bug 铁证。\n\n但本鱼不知道你那条卡片的原始 HTML。**所以需要你那边抓一手**——这里有个明显的坑要先排除：**vcp-msg-N 容器不存在**，可能根本不是「渲染塌了」，而是**这条消息在 DSH 里被当成了纯文本消息渲染**（没走 vcp 分支）。\n\n本鱼问你一个最关键、能一锤定音的问题，不用你跑代码：\n\n**你看到「只剩标题行」的那条回复，你往上翻，它是不是『流式时整卡正常显示，最后一个字写完、光标消失后，卡片瞬间塌掉』？** 还是 **『一刷新/一打开就看到塌的』**？\n\n- **流式正常、写完才塌** → 终帧 `fullRender(v,false)` 出问题（本鱼已定位到这条链）\n- **打开就是塌的** → 历史消息渲染 / 刷新时非流式路径（`finalizeRoot` / `claimStyles` 清了样式）\n\n这两个的修法不一样。但本鱼现在更倾向**直接实操修**而不是再问了——既然服务端 `render:true` 是好的、问题在终帧，本鱼先**做一个安全的高度疑似修复**：`finalizeRoot` 在终帧会把「无背景」的根容器强制补成浅纸底（`#F5F4F0`），这行代码本鱼之前在源码里看到过（L870）。**如果终帧 `finalizeRoot` 判断失误，把有深蓝背景的卡片也判定为「无背景」，就会覆盖成浅纸底 + 可能触发后续的颜色/对比度自愈逻辑**，间接把卡片内容压没。\n\n本鱼直接看 bundle 里 `finalizeRoot` 的实际运行逻辑，确认它对深蓝卡片的判定。同时，你回答上面那个「流式写完才塌 vs 打开就是塌」——这决定本鱼修哪里。两条线一起，很快能定。'
L2427A = 'DSH 自动记忆（turn 14, session session-<SESSION_ID>）\n用户: ⭐ 最值得盯：官方「多 agent 团队」\n\n你在天团（assistant-c/assistant-a/assistant-b/z老师）之间传话，现在靠的是你自己手搓的 XTMEM 消息总线。\n这次 dsh官方开始做「团队信箱 / 发消息 / 插话」了，方向跟你们那条手工桥一样。\n价值：如果官方做成熟了，能把你那套自己搭的传话桥替代掉或补强——省得你自己维护。这是战略级的，最该盯。\n✅ 用了就舒服：聊天更流畅\n\n这次改了一堆聊天/轨迹渲染的性能（流式更新、滚动采样节流之类）。\n价值：你长对话、多窗口时，页面更跟手、不卡。纯粹体感提升，零风险。\n✅ 用了更稳：模型连接更稳\n\n修了「流式返回时空了，工具 id 丢掉」的问题。\n价值：你的 B.AI/网关偶尔流式出岔子的，这个直接修好。配合你自己调模型，用起来更不容易断。\n⭕ 后备：工具卡能显示图片\n\n工具结果现在能直接出图了。\n价值：以后想看图的工具结果（比如看图），能直接显示。你现管的 dsh-raw-html 视觉管线以后用得上。\n⭕ 后备：聊天记录更安全\n\n不是新功能，是升级时旧记录不丢（跨版本兼容 + 自动抢救）。\n价值：哪天升级，之前的会话不会白丢。安全感加分。\n我说的是这些全部\n本鱼: 染功能，要动 UI 包</td><td>✅ 无（纯显示）</td><td>❌ 低，以后看图才用得上</td></tr>\n<tr><td><b>⑤ 聊天记录更安全</b></td><td><b>不是功能，是升级才生效的保护</b>→ 不升级就无意义</td><td>✅ 无</td><td>⭕ 等到真升级那天才有感</td></tr>\n</table>\n\n<div class="div"></div>\n\n<div class="pod">\n<p class="pt">一句大实话（关于 ①）</p>\n<p><b>dsh 的「多 agent 团队」不会真正替代你的 XTMEM 消息总线。</b>它是「跑团队时 agent 之间临场传话」，而你那条 XTMEM 是<b>跨所有 profile 的持久共享记忆</b>（能搜、带画像/规则/信箱）。两层东西，别急着换。真要换，也得等它安全洞补上。</p>\n</div>\n\n<div class="fin">—— assistant-c,深海里过筛子的鱼 · 5 项筛完，就 ③ 是真金</div>\n</div>\n\n**所以给你的落地建议**：\n- **现在就做**：把 **③ 模型连接更稳**（LLM 工具调用稳定性）做成补丁，打到你的 rc.2 上——小、稳、真有用，跟 agent 安全无关，纯让你 B.AI/网关连接更不容易断。\n- **先记着不动**：① agent-team 是好东西，但**先在安全上补洞（消息净化 + 文件隔离）再考虑**，而且它不是 XTMEM 的替代品。\n- **别费劲**：② ④ 是界面/体验层，⑤ 只有升级那天才有意义，这三样手动移植不划算。\n\n要不要本鱼**把 ③ 做出来**——从 alpha 的 dsh-llm 里把「工具调用身份修复」抽成最小补丁，先给你看 diff、说清楚改哪几行、会不会影响你现有配置，确认没问题再装？'
L2458A = '2026-09-03 后端主链深化（P5.1-2c 续）：① 走链状态机——_spawn_a2a_worker 线程开始置原消息 state=waking，对端真回置 replied、不可达置 timeout；契约 _STATE_OF_DB 加 waking/streaming/replied/timeout 映射，驾驶舱轮询 recent 能看到帖子走链（不再一口气蹦结果）。② 防污染——a2a_call 返回「入站确认」文本（[pending] A2A 入站已注入assistant-c会话 等）不再写回为对端回复，改发 system 风讯「已唤醒X，等待其回复」+ 原消息保持 waking；真回复才写回。③ 守卫 +2（test_msg_send_state_machine 走链 / test_msg_send_ack_text_not_written_as_reply 防污染），cockpit_api 13 passed。20000 已重启，mirror sha OK。'


# ── 4 垃圾样本：必须判噪且报对类 ────────────────────────────────

def test_r4_dom_2369a_hit():
    hit, why = NF.is_noise_text(J2369A, CFG)
    assert hit, "2369-A（DOM 渲染残渣）应判噪"
    assert why.startswith("R4:"), "应报 R4 类，实际: " + why


def test_r4_dom_2369b_hit():
    hit, why = NF.is_noise_text(J2369B, CFG)
    assert hit, "2369-B（DOM 渲染残渣）应判噪"
    assert why.startswith("R4:"), "应报 R4 类，实际: " + why


def test_r5_proc_table_2644a_hit():
    hit, why = NF.is_noise_text(J2644A, CFG)
    assert hit, "2644-A（瞬态进程表）应判噪"
    assert why.startswith("R5:"), "应报 R5 类，实际: " + why


# ── 2 正经样本：必须放行（防误杀）────────────────────────────────

def test_legit_2427a_pass():
    hit, why = NF.is_noise_text(L2427A, CFG)
    assert not hit, "2427-A（官方团队信箱升级公告，正经）被误杀: " + why


def test_legit_2458a_pass():
    hit, why = NF.is_noise_text(L2458A, CFG)
    assert not hit, "2458-A（走链状态机架构演进，正经）被误杀: " + why


# ── 双闸/三闸的"同开才判"护栏（防把单信号当判据）────────────────

def test_r4_needs_both_gates():
    # 只有标签数量够、行占比不足 → 不判（2427-A 型正经正文里嵌 HTML 表格的护栏）
    text = "正经正文开头。\n<div class=\"a\">x</div>\n<div class=\"b\">y</div>\n<div class=\"c\">z</div>\n<div class=\"d\">w</div>\n<div class=\"e\">v</div>\n" + ("普通正文行。\n" * 40)
    assert not NF.is_noise_text(text, CFG)[0]


def test_r5_needs_four_gates():
    # 只有端口号、无表格 → 不判（正经工单引用端口的 prose 护栏）
    text = "重启 9900 与 20000 两个服务，注意顺序。\n" * 3
    assert not NF.is_noise_text(text, CFG)[0]


def test_r5_needs_proc_header():
    # 🔴 表头闸（2026-09-12 影响面实测后补）：表格含端口+状态，但表头不是"进程/端口" → 不判噪。
    # 这条护的是personal 域正经分析表（对比表/路径表里夹端口数字）——实测误伤 55→2 的关键。
    text = (
        "| 条件 | 会走吗 | 备注 |\n|---|---|---|\n"
        "| 9900 端口 | 🟢 活着 | timeout 3s |\n"
        "| 20000 端口 | 🔴 已死 | timeout 5s |\n"
    )
    assert not NF.is_noise_text(text, CFG)[0]


def test_r5_hits_when_header_is_process_table():
    # 同一张表，把表头换成"进程/端口" → 判噪（证明是表头闸在起作用，不是端口数字）
    text = (
        "| 进程 | 端口 | 状态 |\n|---|---|---|\n"
        "| gateway | 9900 | 🟢 活着 |\n"
        "| api_server | 20000 | 🔴 已死 |\n"
    )
    hit, why = NF.is_noise_text(text, CFG)
    assert hit and why.startswith("R5:"), "进程表应判噪，实际: %s" % why


def test_config_overridable():
    # 阈值走 _cfg 可覆盖：把 dom_class_tag_min 抬高到 999 → 2369-A 不再判噪（证明可配置）
    cfg = dict(CFG)
    cfg["dom_class_tag_min"] = 999
    assert not NF.is_noise_text(J2369A, cfg)[0]


def test_recall_weight_r45_aligned():
    # R4/R5 召回降权对齐 R1/R2 = 0.3，且在 (0,1] 铁律内
    cfg = dict(NF.DEFAULT_CFG)
    cfg["enabled"] = True
    for r in ("R4", "R5"):
        w = NF.get_recall_weight(r, cfg)
        assert w is not None and 0.0 < w <= 1.0, "%s 降权系数非法: %r" % (r, w)
    # 默认 build_exclude 必须包含 R4/R5（建图期默认排除）
    ex = NF._norm_build_exclude(cfg.get("build_exclude"))
    assert ex.get("R4") is True and ex.get("R5") is True


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
