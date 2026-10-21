# -*- coding: utf-8 -*-
# ================================================================
#   XTMEM · tests/test_xtmem_d31_noise_r6.py · D3-1 扩判据守卫（R6 裸 JSON/抓包堆）
#
#   ▍这是什么
#   maintainer派单《小项二合一》（2026-09-12）件②守卫。R6 是窄闸，本文件同时守两头：
#     该命中的：判卷 2478-B（mem-1e631340 裸 JSON）+ 同族（DOM转储 / genui 组件数据）
#     绝不能误杀的（四类护栏，误杀一条即推倒重来）：
#       a) 判卷正经样本（2427-A / 2458-A）
#       b) 围栏内代码/配置
#       c) 正文夹 JSON 片段的正常文档
#       d) XTMEM 自产资产（实体索引页）
#   ⚠️ 护栏夹具刻意做成"高密度 JSON"——否则会被别的闸挡住而**测不到目标闸**
#      （2026-09-12 首版夹具太弱，反向验证打不中，已按铁律7 加强）。
#   fixture 从快照按 mem-id 自动注入真身（零手抄）。
#
#   ▍修改铁律
#   1. 直载运行时 noise_filter.py（绝对路径，不污染 pytest 会话解析）。
#   2. 只用纯文本 fixture，严禁连生产库（件②零生产写）。
#   3. 反向验证：拆掉任一道闸，本文件必须打红（夹具已按此标准设计）。
#
#   ▍更新记录
#   *更新：2026-09-12 · 创建 + 夹具加强（R6 守卫：必中 3 + 四类护栏 + 窄闸/可配置口径）*
# ================================================================
import importlib.util

from pathlib import Path
_REPO = Path(__file__).resolve().parents[1]
MASTER_NOISE = str(_REPO / "src" / "hermes-plugin" / "memory" / "noise_filter.py")
_spec = importlib.util.spec_from_file_location("xtmem_noise_r6_under_test", MASTER_NOISE)
NF = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(NF)

CFG = dict(NF.DEFAULT_CFG)
CFG["enabled"] = True

# 高密度 JSON 合成体（kv=4、brace=4）——用于精确行使"说话人锚"与"资产护栏"两道闸
_HIGHJSON = '{\n"alpha": {"x": 1},\n"beta": 2,\n"gamma": 3,\n"delta": 4\n}'

J2478B = 'DSH 自动记忆（turn 29, session session-<SESSION_ID>）\n用户: {\\n  "stable": "object",\\n  "ownedStyleCount": 0,\\n  "cardElement": null,\\n  "totalStyles": 137,\\n  "styleHeads": [\\n    "dsh-raw-html:\\\\n/* 需求 16：手机端设置页改造。属性由 settings-mobile.t",\\n    "dsh-raw-html:\\\\n@media (pointer: coarse) {\\\\n  [data-slot",\\n    "@deepseek-ai/dsh-client-ui-directory-picker-browse:.ZuhsRW_dialog.ZuhsRW_dialog{--dsh-scrol",\\n    "none:@font-face{font-family:\'Lanxi-WenKai\';sr"\\n  ],\\n  "renderToggle": "未找到"\\n}`\n本鱼: er`。本鱼直接在浏览器里用 `render()` 这个函数，把**你那条卡片的原始 HTML 喂进去**，看它返回什么——如果返回 null 或空，就是引擎 bug 铁证。\n\n但本鱼不知道你那条卡片的原始 HTML。**所以需要你那边抓一手**——这里有个明显的坑要先排除：**vcp-msg-N 容器不存在**，可能根本不是「渲染塌了」，而是**这条消息在 DSH 里被当成了纯文本消息渲染**（没走 vcp 分支）。\n\n本鱼问你一个最关键、能一锤定音的问题，不用你跑代码：\n\n**你看到「只剩标题行」的那条回复，你往上翻，它是不是『流式时整卡正常显示，最后一个字写完、光标消失后，卡片瞬间塌掉』？** 还是 **『一刷新/一打开就看到塌的』**？\n\n- **流式正常、写完才塌** → 终帧 `fullRender(v,false)` 出问题（本鱼已定位到这条链）\n- **打开就是塌的** → 历史消息渲染 / 刷新时非流式路径（`finalizeRoot` / `claimStyles` 清了样式）\n\n这两个的修法不一样。但本鱼现在更倾向**直接实操修**而不是再问了——既然服务端 `render:true` 是好的、问题在终帧，本鱼先**做一个安全的高度疑似修复**：`finalizeRoot` 在终帧会把「无背景」的根容器强制补成浅纸底（`#F5F4F0`），这行代码本鱼之前在源码里看到过（L870）。**如果终帧 `finalizeRoot` 判断失误，把有深蓝背景的卡片也判定为「无背景」，就会覆盖成浅纸底 + 可能触发后续的颜色/对比度自愈逻辑**，间接把卡片内容压没。\n\n本鱼直接看 bundle 里 `finalizeRoot` 的实际运行逻辑，确认它对深蓝卡片的判定。同时，你回答上面那个「流式写完才塌 vs 打开就是塌」——这决定本鱼修哪里。两条线一起，很快能定。'
J_DOMDUMP = 'DSH 自动记忆（turn 23, session session-<SESSION_ID>）\n用户: \'[\\n  {\\n    "id": "vcp-msg-124",\\n    "height": 752,\\n    "rootBg": "rgb(8, 20, 34) none repeat scroll 0% 0% ",\\n    "childCount": 3,\\n    "firstChildren": [\\n      "STYLE h=0 bg=rgba(0, 0, 0, 0) n",\\n      "sea h=117 bg=rgba(0, 0, 0, 0) l",\\n      "bay h=635 bg=rgba(0, 0, 0, 0) n"\\n    ],\\n    "datavcp": "无"\\n  }\\n]\'\n本鱼: .bay\');\n  if(!bay) return \'.bay 不存在\';\n  var cells=bay.querySelectorAll(\'.cell, [class*=cell], p, h2, li\');\n  var out={bayHeight:bay.offsetHeight, bayBg:getComputedStyle(bay).background.slice(0,30), items:[]};\n  for(var i=0;i<cells.length&&i<6;i++){\n    var c=cells[i];\n    var cs=getComputedStyle(c);\n    out.items.push({\n      tag:(c.className||c.tagName).toString().slice(0,18),\n      visible: c.offsetHeight>0 && c.offsetWidth>0,\n      color: cs.color.slice(0,22),\n      bg: cs.background.slice(0,22),\n      fontSize: cs.fontSize,\n      text: (c.textContent||\'\').slice(0,24)\n    });\n  }\n  out.bayChildCount=bay.children.length;\n  return JSON.stringify(out,null,2);\n})()\n```\n\n贴给本鱼。本鱼重点看：**`.bay` 里的 `.cell` 文字颜色是不是异常（比如被覆盖成浅色）**、**fontSize 是不是被压小了**、**有没有显示高度但是内容透明**。这一下能定病根到底在「作用域替换」还是「皮肤/字体层覆盖」。'
J_GENUI = 'DSH 自动记忆（turn 14, session session-<SESSION_ID>）\n用户: [genui-action] notify-decision。用户刚刚在界面中触发了动作 "notify-decision"，请根据组件数据执行相应操作，并用 dsh-ui 输出更新后的界面。 组件数据: {"type":"submit","answers":{"notify":"A · 维持现状"},"total":1,"answered":1}\n本鱼: 拍板落库完毕（mem-a4003a2e）。这轮的账全平了：\n\n| 事项 | 状态 |\n|---|---|\n| ① 深海工作台开场卡 | ✅ 转正（场景条目+记忆，下场自动生效） |\n| ③ 拍板卡 | ✅ 定稿（不带正确答案版）+ 实战首单完成 |\n| 通知通道 | ✅ 拍板 A 维持现状，B 方案挂进待办池 |\n| ② 交付卡 / ④ 航海日志 | 规矩已立，遇到场景自然触发 |\n| ⑤ 双色谱 / ⑥ 呼吸动效 | 冻结中，等你哪天想动界面再捞 |\n\n咕噜咕噜，今天从装插件到字体换装再到美学体系，鱼生充实。还有什么要本鱼干的吗？没有的话本鱼就要开始**战略性待机**了——鳍都收好了。'
L2427A = 'DSH 自动记忆（turn 14, session session-<SESSION_ID>）\n用户: ⭐ 最值得盯：官方「多 agent 团队」\n\n你在天团（assistant-c/assistant-a/assistant-b/z老师）之间传话，现在靠的是你自己手搓的 XTMEM 消息总线。\n这次 dsh官方开始做「团队信箱 / 发消息 / 插话」了，方向跟你们那条手工桥一样。\n价值：如果官方做成熟了，能把你那套自己搭的传话桥替代掉或补强——省得你自己维护。这是战略级的，最该盯。\n✅ 用了就舒服：聊天更流畅\n\n这次改了一堆聊天/轨迹渲染的性能（流式更新、滚动采样节流之类）。\n价值：你长对话、多窗口时，页面更跟手、不卡。纯粹体感提升，零风险。\n✅ 用了更稳：模型连接更稳\n\n修了「流式返回时空了，工具 id 丢掉」的问题。\n价值：你的 B.AI/网关偶尔流式出岔子的，这个直接修好。配合你自己调模型，用起来更不容易断。\n⭕ 后备：工具卡能显示图片\n\n工具结果现在能直接出图了。\n价值：以后想看图的工具结果（比如看图），能直接显示。你现管的 dsh-raw-html 视觉管线以后用得上。\n⭕ 后备：聊天记录更安全\n\n不是新功能，是升级时旧记录不丢（跨版本兼容 + 自动抢救）。\n价值：哪天升级，之前的会话不会白丢。安全感加分。\n我说的是这些全部\n本鱼: 染功能，要动 UI 包</td><td>✅ 无（纯显示）</td><td>❌ 低，以后看图才用得上</td></tr>\n<tr><td><b>⑤ 聊天记录更安全</b></td><td><b>不是功能，是升级才生效的保护</b>→ 不升级就无意义</td><td>✅ 无</td><td>⭕ 等到真升级那天才有感</td></tr>\n</table>\n\n<div class="div"></div>\n\n<div class="pod">\n<p class="pt">一句大实话（关于 ①）</p>\n<p><b>dsh 的「多 agent 团队」不会真正替代你的 XTMEM 消息总线。</b>它是「跑团队时 agent 之间临场传话」，而你那条 XTMEM 是<b>跨所有 profile 的持久共享记忆</b>（能搜、带画像/规则/信箱）。两层东西，别急着换。真要换，也得等它安全洞补上。</p>\n</div>\n\n<div class="fin">—— assistant-c,深海里过筛子的鱼 · 5 项筛完，就 ③ 是真金</div>\n</div>\n\n**所以给你的落地建议**：\n- **现在就做**：把 **③ 模型连接更稳**（LLM 工具调用稳定性）做成补丁，打到你的 rc.2 上——小、稳、真有用，跟 agent 安全无关，纯让你 B.AI/网关连接更不容易断。\n- **先记着不动**：① agent-team 是好东西，但**先在安全上补洞（消息净化 + 文件隔离）再考虑**，而且它不是 XTMEM 的替代品。\n- **别费劲**：② ④ 是界面/体验层，⑤ 只有升级那天才有意义，这三样手动移植不划算。\n\n要不要本鱼**把 ③ 做出来**——从 alpha 的 dsh-llm 里把「工具调用身份修复」抽成最小补丁，先给你看 diff、说清楚改哪几行、会不会影响你现有配置，确认没问题再装？'
L2458A = '2026-09-03 后端主链深化（P5.1-2c 续）：① 走链状态机——_spawn_a2a_worker 线程开始置原消息 state=waking，对端真回置 replied、不可达置 timeout；契约 _STATE_OF_DB 加 waking/streaming/replied/timeout 映射，驾驶舱轮询 recent 能看到帖子走链（不再一口气蹦结果）。② 防污染——a2a_call 返回「入站确认」文本（[pending] A2A 入站已注入assistant-c会话 等）不再写回为对端回复，改发 system 风讯「已唤醒X，等待其回复」+ 原消息保持 waking；真回复才写回。③ 守卫 +2（test_msg_send_state_machine 走链 / test_msg_send_ack_text_not_written_as_reply 防污染），cockpit_api 13 passed。20000 已重启，mirror sha OK。'


def test_r6_hits_2478b():
    """必中：判卷 2478-B（裸 JSON 抓包堆）"""
    hit, why = NF.is_noise_text(J2478B, CFG)
    assert hit, "2478-B 必须命中"
    assert why.startswith("R6:"), "应报 R6 类，实际: " + why


def test_r6_hits_same_family():
    """必中：同族（DOM 转储 / genui 组件数据）"""
    for name, t in (("dsh DOM dump", J_DOMDUMP), ("genui 组件数据", J_GENUI)):
        hit, why = NF.is_noise_text(t, CFG)
        assert hit and why.startswith("R6:"), "%s 应判噪，实际 %s" % (name, why)


def test_guard_legit_samples_pass():
    """护栏 a：判卷正经样本必须放行"""
    for name, t in (("2427-A", L2427A), ("2458-A", L2458A)):
        hit, why = NF.is_noise_text(t, CFG)
        assert not hit, "正经样本 %s 被误杀: %s" % (name, why)


def test_guard_speaker_anchor_required():
    """护栏核心：高密度 JSON 但**无**"说话人标签后紧跟"→ 必须放行。
    （实测：去掉此锚会误杀 17 条正经片段；本夹具密度已够，确保真在测这道闸）"""
    t = "配置说明：先讲背景，再贴样例。\n" + _HIGHJSON
    hit, why = NF.is_noise_text(t, CFG)
    assert not hit, "无说话人锚的高密度 JSON 被误杀: " + why


def test_guard_self_asset_pass_highjson():
    """护栏 d：自产资产（实体索引页）**即使含高密度 JSON + 说话人锚** 也必须放行。
    ⚠️ 夹具必须带锚，否则走不到"资产豁免"这道闸（反向验证会打不中——2026-09-12 实测修正）。"""
    t = "# 实体 · Ollama\n用户: " + _HIGHJSON
    hit, why = NF.is_noise_text(t, CFG)
    assert not hit, "自产资产被误杀: " + why


def test_guard_fenced_code_passes():
    """护栏 b：围栏内代码/配置必须放行"""
    t = '工单：\n```json\n{\n"a": {"x": 1},\n"b": 2,\n"c": 3,\n"d": 4\n}\n```\n以上。'
    assert not NF.is_noise_text(t, CFG)[0]


def test_guard_inline_json_doc_passes():
    """护栏 c：正文夹 JSON 片段的正常文档必须放行"""
    t = '回执：配置项 "mode": "on"，另外 "level": 3 已改，"retry": 2 生效，详见工单。'
    assert not NF.is_noise_text(t, CFG)[0]


def test_r6_configurable_off():
    cfg = dict(CFG)
    cfg["json_blob_enabled"] = False
    assert not NF.is_noise_text(J2478B, cfg)[0]


def test_r6_in_rules_and_weights():
    assert "R6" in NF.RULES
    ex = NF._norm_build_exclude(NF.DEFAULT_CFG.get("build_exclude"))
    w = NF.get_recall_weight("R6", NF.DEFAULT_CFG)
    assert ex.get("R6") is True, "R6 应默认进建图期排除"
    assert w is not None and 0.0 < w <= 1.0, "R6 降权须 ∈(0,1]，实际 %r" % w


if __name__ == "__main__":
    raise SystemExit(__import__("pytest").main([__file__, "-v"]))
