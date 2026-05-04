import asyncio
import re
import time
import json
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import ProviderRequest
import astrbot.api.message_components as Comp

# ============================================================
# 模块级状态
# ============================================================
_mute_state: dict = {}           # 插件内部的临时拒绝回复状态表，key -> {"until": float, "reason": str, "set_by": str}
_delayed_retrigger: dict = {}    # umo -> [asyncio.Task, ...]
_MAX_RETRIGGER_DEPTH = 3

# ============================================================
# 标签模式（按长度从长到短排列，避免匹配歧义）
# ============================================================
_TAG_PATTERNS = [
    (re.compile(r'<no_reply_(\d+)_(\S+?)>'), 'no_reply_user'),
    (re.compile(r'<no_reply_(\d+)>'),        'no_reply_time'),
    (re.compile(r'<no_reply>'),              'no_reply'),
    (re.compile(r'<delay_reply_(\d+)>'),     'delay_reply'),
]

# ============================================================
# 为允许使用该功能的会话注入的 system prompt 说明
# ============================================================
_TAG_INSTRUCTIONS = """

## ⚠️ 回复控制标签（重要）

你可以在回复文本中嵌入以下标签来精确控制回复行为。
标签会被系统自动识别并移除，用户不会看到标签本身。

| 标签格式 | 作用 |
|----------|------|
| `<no_reply>` | 不回复当前这条消息 |
| `<no_reply_秒数>` | 在指定秒数内不回复该会话的任何消息 |
| `<no_reply_秒数_用户ID>` | 在指定秒数内拒绝回复该用户的消息 |
| `<delay_reply_秒数>` | 延迟指定秒数后系统重新触发你生成回复 |

规则:
1. 标签可放在回复任意位置，系统自动移除。
2. <no_reply> 系列: 其余文字不会发送给用户。
3. <delay_reply>: 当前回复不会发送，延迟结束后重新调用你生成回复。
4. 秒数建议范围: 10-3600。
5. 用户 ID 需从对话上下文中获取（如消息中的 user_id），并显式填入标签。
   若无法确定用户 ID，请改用 <no_reply_time>。
6. 每次只能使用一种标签。若输出多个标签，系统按优先级选取一个生效：
   no_reply_user > no_reply_time > no_reply > delay_reply

决策指南:
- 用户消息不当 → `<no_reply>`
- 需要让用户冷静 → `<no_reply_600>`
- 某人频繁骚扰 → `<no_reply_1800_用户ID>` 以在一段时间内不再回复该用户
- 需时间查询 → `<delay_reply_30>`
- 需较长时间处理 → `<delay_reply_120>`
"""


# ============================================================
# 辅助函数
# ============================================================

def _extract_plain_text(chain: list) -> str:
    parts = []
    for seg in (chain or []):
        if isinstance(seg, Comp.Plain):
            parts.append(seg.text)
    return "".join(parts)


def _parse_tags(text: str) -> list[dict]:
    """解析文本中的所有控制标签，返回按出现位置排序的结果列表。"""
    results = []
    for pattern, tag_type in _TAG_PATTERNS:
        for m in pattern.finditer(text):
            entry = {"type": tag_type, "start": m.start(), "end": m.end(), "raw": m.group(0)}
            groups = m.groups()
            if tag_type == 'no_reply_user':
                entry["time"] = int(groups[0])
                entry["target"] = groups[1]
            elif tag_type == 'no_reply_time':
                entry["time"] = int(groups[0])
            elif tag_type == 'delay_reply':
                entry["time"] = int(groups[0])
            results.append(entry)
    results.sort(key=lambda x: (x["start"], -(x["end"] - x["start"])))
    return results


def _strip_tags(text: str) -> str:
    clean = text
    for pattern, _ in _TAG_PATTERNS:
        clean = pattern.sub("", clean)
    return clean.strip()


def _normalize_seconds(value, default: int = 0) -> int:
    """将输入转换为非负整数秒数，失败时返回默认值。

    注意：本函数仅保证下限（非负），不设置上限。上限由各调用处根据配置的
    max_delay_seconds / max_mute_minutes 控制。
    """
    try:
        sec = int(value)
    except (TypeError, ValueError):
        return default
    return max(sec, 0)


def _cleanup_expired_mutes() -> None:
    """立即清理已过期的拒绝回复状态，避免状态表长期残留脏数据。"""
    now = time.time()
    expired = [k for k, v in _mute_state.items() if now >= v.get("until", 0)]
    for k in expired:
        del _mute_state[k]


def _select_effective_tag(tags: list[dict]) -> dict | None:
    """从多个标签中选出最终生效的标签，避免冲突动作同时执行。

    优先级：
      no_reply_user > no_reply_time > no_reply > delay_reply
    同优先级下按出现位置靠前者优先。
    """
    if not tags:
        return None
    priority = {
        "no_reply_user": 4,
        "no_reply_time": 3,
        "no_reply": 2,
        "delay_reply": 1,
    }
    return sorted(tags, key=lambda t: (-priority.get(t["type"], 0), t["start"]))[0]


def _register_delayed_task(umo: str, task: asyncio.Task) -> None:
    """登记延迟重触发任务，便于状态查询、取消与卸载清理。"""
    _delayed_retrigger.setdefault(umo, []).append(task)


def _match_mute(event: AstrMessageEvent, target: str) -> bool:
    """检查当前事件是否命中插件内部的拒绝回复目标。

    仅用于控制插件是否继续回复，不代表平台层面的真实禁言。
    这里仅接受精确匹配，避免前缀或后缀规则导致误命中。

    注意：该函数目前只被 /refuse_status 和 /refuse_cancel 命令使用，
    _resolve_mute() 已改为直接键查找，不再依赖此函数。
    """
    umo = event.unified_msg_origin
    sid = event.get_sender_id()
    return target == umo or target == sid


def _resolve_mute(event: AstrMessageEvent) -> tuple[dict | None, str]:
    """返回当前事件命中的最高优先级拒绝回复状态与作用域。

    scope 取值如下:
      - "session" — 临时拒绝回复整个会话（按 umo 匹配）
      - "user"    — 临时拒绝回复特定用户（按 sender_id 匹配）
      - "none"    — 未命中任何拒绝回复状态

    当用户级与会话级状态同时存在时，优先返回用户级状态。
    """
    _cleanup_expired_mutes()
    umo = event.unified_msg_origin
    sid = event.get_sender_id()

    user_entry = _mute_state.get(sid)
    if user_entry and time.time() < user_entry.get("until", 0):
        return user_entry, "user"

    session_entry = _mute_state.get(umo)
    if session_entry and time.time() < session_entry.get("until", 0):
        return session_entry, "session"

    return None, "none"


def _resolve_name(event: AstrMessageEvent, uid: str) -> str:
    """将 UID 解析为显示名称，解析失败时回退为 UID 本身。"""
    if uid == event.get_sender_id():
        name = event.get_sender_name()
        if name and name != uid:
            return name
    return uid


def _check_feature(ctx: Context, event: AstrMessageEvent) -> bool:
    """检查当前会话是否允许使用拒绝回复功能。"""
    try:
        stars = ctx.get_all_stars()
        for s in stars:
            if s.__class__.__name__ == 'LLMRefusePlugin' and hasattr(s, 'config') and s.config:
                cfg = s.config
                mode = cfg.get("feature_mode", "none")
                if mode == "none":
                    return True
                uid_text = cfg.get("feature_uid_list", "").strip()
                if not uid_text:
                    return True
                entries = [line.strip() for line in uid_text.split("\n") if line.strip()]
                if not entries:
                    return True
                umo = event.unified_msg_origin
                sid = event.get_sender_id()
                matched = any(e == umo or e == sid for e in entries)
                if mode == "whitelist":
                    return matched
                elif mode == "blacklist":
                    return not matched
    except Exception:
        pass
    return True


def _get_plugin_cfg(ctx: Context, key: str, default=None):
    """从插件实例中读取配置项，返回第一个 LLMRefusePlugin 实例的值。

    注意：当前实现假定只有一个插件实例，多实例环境可能返回非预期配置。
    """
    try:
        stars = ctx.get_all_stars()
        for s in stars:
            if s.__class__.__name__ == 'LLMRefusePlugin' and hasattr(s, 'config') and s.config:
                return s.config.get(key, default)
    except Exception:
        pass
    return default


# ============================================================
# 构造带引用和格式化文案的回复链
# ============================================================

def _build_reply_chain(
    event: AstrMessageEvent,
    enabled: bool,
    template: str,
    **placeholders,
) -> list | None:
    """构造拒绝回复提示所使用的消息链。

    返回值为消息组件列表；若当前场景未启用提示回复，则返回 None。
    生成的消息链会尽量引用触发本次行为的原消息，并附加格式化后的提示文案。
    """
    if not enabled:
        return None  # 不作回复：不发送任何回复

    # 格式化提示文案
    try:
        text = template.format(**placeholders)
    except (KeyError, ValueError):
        text = template

    if not text or not text.strip():
        return None

    # 构造回复链：先引用触发消息，再附加提示文本
    chain = []
    try:
        msg_id = event.message_obj.message_id if event.message_obj else None
        if msg_id:
            chain.append(Comp.Reply(id=str(msg_id)))
    except Exception:
        pass

    chain.append(Comp.Plain(text))
    return chain


# ============================================================
# 延迟后重新触发 LLM
# ============================================================

async def _retrigger_llm(
    ctx: Context,
    umo: str,
    delay: float,
    depth: int = 0,
):
    """等待指定秒数后重新触发 LLM，并再次扫描输出中的控制标签。"""
    await asyncio.sleep(delay)

    if depth >= _MAX_RETRIGGER_DEPTH:
        logger.warning(f"[Retrigger] max depth {_MAX_RETRIGGER_DEPTH} reached, aborting")
        return

    logger.info(f"[Retrigger] umo={umo} depth={depth} re-triggering LLM ...")
    try:
        provider_id = await ctx.get_current_chat_provider_id(umo)
        if not provider_id:
            logger.error(f"[Retrigger] no provider for {umo}")
            return

        conv_mgr = ctx.conversation_manager
        curr_cid = await conv_mgr.get_curr_conversation_id(umo)
        history = []
        conv = None
        if curr_cid:
            conv = await conv_mgr.get_conversation(umo, curr_cid)
            if conv and conv.history:
                try:
                    history = json.loads(conv.history)
                except Exception:
                    history = []

        user_contexts = []
        for item in (history[-30:] if history else []):
            if isinstance(item, dict):
                user_contexts.append(item)
        # 从会话获取人格设定；若读不到则不带人格，由上下文历史保持人格
        system_prompt = None
        try:
            persona_id = getattr(conv, "persona_id", None) if conv else None
            if persona_id:
                persona = ctx.persona_manager.get_persona(persona_id)
                if persona:
                    if hasattr(persona, "prompt"):
                        system_prompt = persona.prompt
                    else:
                        system_prompt = persona.get("prompt") if isinstance(persona, dict) else None
        except Exception:
            pass

        llm_resp = await ctx.llm_generate(
            chat_provider_id=provider_id,
            prompt="(请根据以上对话历史中最后一条用户消息，自然地回复该消息。注意：不要使用任何回复控制标签。)",
            system_prompt=system_prompt,
            contexts=user_contexts if user_contexts else None,
        )

        text = llm_resp.completion_text if llm_resp else ""
        if not text:
            logger.warning(f"[Retrigger] empty response")
            return

        # 扫描本次重新生成结果中的嵌套标签
        tags = _parse_tags(text)
        effective_tag = _select_effective_tag(tags)
        if effective_tag and depth + 1 < _MAX_RETRIGGER_DEPTH:
            tag_type = effective_tag["type"]
            if tag_type == "no_reply":
                logger.info(f"[Retrigger] nested no_reply, not sending")
                return
            elif tag_type == "no_reply_time":
                duration = _normalize_seconds(effective_tag.get("time"), 300)
                max_mute = int(_get_plugin_cfg(ctx, "max_mute_minutes", 1440)) * 60
                duration = min(duration, max_mute)
                _mute_state[umo] = {
                    "until": time.time() + duration,
                    "reason": f"LLM refuse-reply {duration}s",
                    "set_by": umo,
                }
                logger.info(f"[Retrigger] nested refuse-reply session: {umo} {duration}s")
                return
            elif tag_type == "no_reply_user":
                duration = _normalize_seconds(effective_tag.get("time"), 300)
                max_mute = int(_get_plugin_cfg(ctx, "max_mute_minutes", 1440)) * 60
                duration = min(duration, max_mute)
                target_uid = (effective_tag.get("target") or "").strip()
                if not target_uid:
                    logger.warning(f"[Retrigger] nested no_reply_user missing target, ignored | umo={umo}")
                    return
                _mute_state[target_uid] = {
                    "until": time.time() + duration,
                    "reason": f"LLM refuse-reply-user {duration}s",
                    "set_by": umo,
                }
                logger.info(f"[Retrigger] nested refuse-reply user: {target_uid} {duration}s")
                return
            elif tag_type == "delay_reply":
                delay2 = _normalize_seconds(effective_tag.get("time"), 60)
                max_delay = int(_get_plugin_cfg(ctx, "max_delay_seconds", 3600))
                delay2 = min(delay2, max_delay)
                task = asyncio.create_task(_retrigger_llm(ctx, umo, delay2, depth + 1))
                _register_delayed_task(umo, task)
                logger.info(f"[Retrigger] nested delay_reply: {delay2}s")
                return

        clean = _strip_tags(text) if tags else text
        if clean:
            chain = MessageChain().message(clean)
            await ctx.send_message(umo, chain)
            logger.info(f"[Retrigger] sent -> {umo}")

    except Exception as e:
        logger.error(f"[Retrigger] failed: {e}", exc_info=True)


# ============================================================
# 插件主类
# ============================================================

class LLMRefusePlugin(Star):
    """LLM 自主拒绝回复插件（纯文本标签模式）

    标签格式:
      <no_reply>                     — 拒绝回复当前消息
      <no_reply_秒数>                 — 拒绝回复当前会话 N 秒
      <no_reply_秒数_用户ID>          — 在 N 秒内拒绝回复指定用户
      <delay_reply_秒数>             — 延迟 N 秒后重新触发 LLM

    每种标签可独立配置:
      - 是否回复提示消息（默认关闭，不提示拒绝）
      - 自定义回复文案（支持 {time}、{target} 占位符）
      - 回复时自动引用触发消息
    """

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("[LLMRefuse] 插件已启动（文本标签模式）")

    # --------------------------------------------------------
    # 周期性清理过期拒绝回复状态与已完成任务
    # --------------------------------------------------------
    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(300)
            now = time.time()
            expired = [k for k, v in _mute_state.items() if now >= v["until"]]
            for k in expired:
                logger.info(f"[LLMRefuse] refuse-reply expired: {k}")
                del _mute_state[k]
            for umo in list(_delayed_retrigger.keys()):
                _delayed_retrigger[umo] = [
                    t for t in _delayed_retrigger[umo] if not t.done()
                ]
                if not _delayed_retrigger[umo]:
                    del _delayed_retrigger[umo]

    # --------------------------------------------------------
    # Hook 1：注入标签说明并检查拒绝回复状态
    # --------------------------------------------------------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        umo = event.unified_msg_origin

        # --- 检查是否命中拒绝回复状态（对所有消息生效） ---
        mute_entry, scope = _resolve_mute(event)
        if mute_entry:
            remaining = int(mute_entry["until"] - time.time())
            logger.info(f"[LLMRefuse] muted ({scope}): {umo}, remaining={remaining}s")
            event.stop_event()
            # 根据作用域选择对应的提示模板
            if scope == "user":
                # 用户级拒绝回复状态：使用 no_reply_user 配置
                chain = _build_reply_chain(
                    event,
                    enabled=self.config.get("no_reply_user_enabled", False),
                    template=self.config.get("no_reply_user_text", "你已被拒绝回复，{time}s后恢复"),
                    time=remaining,
                )
            else:
                # 会话级拒绝回复状态：使用 no_reply_time 配置
                chain = _build_reply_chain(
                    event,
                    enabled=self.config.get("no_reply_time_enabled", False),
                    template=self.config.get("no_reply_time_text", "该会话已被拒绝回复，{time}s后恢复"),
                    time=remaining,
                )
            if chain:
                result = event.make_result()
                result.chain = chain
                await event.send(result)
            return

        # --- 命令消息（以 / 开头）直接阻止，不进入 LLM 流程 ---
        user_text = event.get_plain_text()
        if user_text and user_text.strip().startswith("/"):
            event.stop_event()
            return

        # --- 为允许使用该功能的会话注入标签说明 ---
        if _check_feature(self.context, event):
            req.system_prompt = (req.system_prompt or "") + _TAG_INSTRUCTIONS

    # --------------------------------------------------------
    # Hook 2：解析 LLM 输出中的控制标签
    # --------------------------------------------------------
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin

        if not _check_feature(self.context, event):
            return

        # --- 命令消息不解析标签，直接退出 ---
        user_text = event.get_plain_text()
        if user_text and user_text.strip().startswith("/"):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        text = _extract_plain_text(result.chain)
        if not text:
            return

        tags = _parse_tags(text)
        if not tags:
            return

        logger.info(f"[LLMRefuse] tags detected: {[{t['type']: t.get('raw','')} for t in tags]}")

        effective_tag = _select_effective_tag(tags)
        if not effective_tag:
            return

        final_chain = None
        tag_type = effective_tag["type"]

        if tag_type == "no_reply":
            final_chain = _build_reply_chain(
                event,
                enabled=self.config.get("no_reply_enabled", False),
                template=self.config.get("no_reply_text", "你已被拒绝回复"),
            )
            logger.info(f"[LLMRefuse] no_reply -> {umo}")

        elif tag_type == "no_reply_time":
            duration = _normalize_seconds(effective_tag.get("time"), 300)
            max_mute = int(self.config.get("max_mute_minutes", 1440)) * 60
            duration = min(duration, max_mute)
            _mute_state[umo] = {
                "until": time.time() + duration,
                "reason": f"LLM refuse-reply {duration}s",
                "set_by": umo,
            }
            final_chain = _build_reply_chain(
                event,
                enabled=self.config.get("no_reply_time_enabled", False),
                template=self.config.get("no_reply_time_text", "该会话已被拒绝回复，{time}s后恢复"),
                time=duration,
            )
            logger.info(f"[LLMRefuse] no_reply_time={duration}s -> {umo}")

        elif tag_type == "no_reply_user":
            duration = _normalize_seconds(effective_tag.get("time"), 300)
            max_mute = int(self.config.get("max_mute_minutes", 1440)) * 60
            duration = min(duration, max_mute)
            target_uid = (effective_tag.get("target") or "").strip()
            if not target_uid:
                logger.warning(f"[LLMRefuse] no_reply_user missing target, ignored | umo={umo}")
                result.chain = []
                return
            target_name = _resolve_name(event, target_uid)
            _mute_state[target_uid] = {
                "until": time.time() + duration,
                "reason": f"LLM refuse-reply-user {duration}s",
                "set_by": umo,
            }
            final_chain = _build_reply_chain(
                event,
                enabled=self.config.get("no_reply_user_enabled", False),
                template=self.config.get("no_reply_user_text", "你已被拒绝回复，{time}s后恢复"),
                time=duration,
                target=target_name,
            )
            logger.info(f"[LLMRefuse] no_reply_user={duration}s target={target_uid} name={target_name}")

        elif tag_type == "delay_reply":
            delay = _normalize_seconds(effective_tag.get("time"), 60)
            max_delay = int(self.config.get("max_delay_seconds", 3600))
            delay = min(delay, max_delay)

            # 创建延迟重触发任务
            task = asyncio.create_task(
                _retrigger_llm(self.context, umo, delay, depth=0)
            )
            _register_delayed_task(umo, task)

            final_chain = _build_reply_chain(
                event,
                enabled=self.config.get("delay_reply_enabled", False),
                template=self.config.get("delay_reply_text", "将在{time}s后回复你"),
                time=delay,
            )
            logger.info(f"[LLMRefuse] delay_reply={delay}s -> {umo}")

        # 应用最终回复链：若启用提示则发送预设回复，否则清空为不作回复
        result.chain = final_chain if final_chain is not None else []

    # --------------------------------------------------------
    # 用户指令
    # --------------------------------------------------------
    @filter.command("refuse_status")
    async def cmd_status(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        now = time.time()
        lines = ["[拒绝回复插件] 状态信息", ""]

        mode = self.config.get("feature_mode", "none")
        can_use = _check_feature(self.context, event)
        lines.append(
            f"[功能名单] {mode} | 当前会话 {'可用标签' if can_use else '不可用标签'}"
        )

        lines.append("\n[匹配当前会话的拒绝回复状态]")
        found = False
        for key, entry in _mute_state.items():
            if now < entry["until"] and _match_mute(event, key):
                remaining = int(entry["until"] - now)
                lines.append(f"  - {key} | 剩余 {remaining}s | {entry['reason']}")
                found = True
        if not found:
            lines.append("  (无)")

        lines.append("\n📜 **全部活跃拒绝回复状态:**")
        active = {k: v for k, v in _mute_state.items() if now < v["until"]}
        if active:
            for key, entry in active.items():
                remaining = int(entry["until"] - now)
                lines.append(f"  - {key} | {remaining}s | {entry['reason']}")
        else:
            lines.append("  (无)")

        lines.append("\n⏳ **延迟重触发任务:**")
        if umo in _delayed_retrigger:
            pending = [t for t in _delayed_retrigger[umo] if not t.done()]
            lines.append(f"  {len(pending)} 个待触发")
        else:
            lines.append("  (无)")

        event.stop_event()
        yield event.plain_result("\n".join(lines))

    @filter.command("refuse_cancel")
    async def cmd_cancel(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        cancelled_mutes = 0
        cancelled_tasks = 0

        for key in list(_mute_state.keys()):
            if _match_mute(event, key):
                del _mute_state[key]
                cancelled_mutes += 1
                logger.info(f"[LLMRefuse] manual cancel refuse-reply: {key}")

        if umo in _delayed_retrigger:
            for task in _delayed_retrigger[umo]:
                if not task.done():
                    task.cancel()
                    cancelled_tasks += 1
            del _delayed_retrigger[umo]

        event.stop_event()
        yield event.plain_result(
            f"已取消 {cancelled_mutes} 条拒绝回复状态, "
            f"终止 {cancelled_tasks} 个延迟任务。"
        )

    async def terminate(self):
        if hasattr(self, '_cleanup_task'):
            self._cleanup_task.cancel()
        for tasks in _delayed_retrigger.values():
            for task in tasks:
                if not task.done():
                    task.cancel()
        logger.info("[LLMRefuse] 插件已卸载")
