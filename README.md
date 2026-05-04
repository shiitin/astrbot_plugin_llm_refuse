# astrbot_plugin_llm_refuse LLM 自主拒绝回复插件

<div align="center">

![](https://img.shields.io/badge/版本-v1.0.0-blue)
![](https://img.shields.io/badge/作者-shiitin-39c5bb)

</div>

允许 LLM 自主控制回复行为：拒绝本次回复、临时拒绝回复当前会话、临时拒绝回复指定用户、延迟重触发回复。支持使用范围白名单/黑名单，以上每种场景可独立配置是否回复提示及自定义文案。


## 兼容性

- AstrBot >= 4.5.7
- 当前适配平台：aiocqhttp

## 快速开始
- AstrBot WebUI → 插件市场 → 搜索 **LLM 自主拒绝回复** 并安装(待开放)
- 在该仓库下载压缩包 → AstrBot WebUI → 上传压缩包

## 四种标签

LLM 在回复中嵌入以下标签即可触发对应行为。标签放在回复中任意位置，系统自动识别并触发，用户不可见。

| 标签 | 作用域 | 示例 | 说明 |
|------|--------|------|------|
| `<no_reply>` | 当前消息 | `<no_reply>` | 拒绝回复当前这一条消息 |
| `<no_reply_{秒数}>` | 整个会话 | `<no_reply_600>` | 所在会话（群聊/私聊）在 N 秒内被拒绝回复 |
| `<no_reply_{秒数}_{用户ID}>` | 指定用户 | `<no_reply_1800_123456>` | 在 N 秒内拒绝回复指定用户的消息 |
| `<delay_reply_{秒数}>` | 当前消息 | `<delay_reply_60>` | 延迟 N 秒后重新触发 LLM 生成回复 |

> **标签优先级**：当 LLM 一次输出多个标签时，系统按以下优先级只选取一个生效，其余忽略：
> `no_reply_user` > `no_reply_time` > `no_reply` > `delay_reply`
> 例如：同时输出 `<delay_reply_30>` 和 `<no_reply>` 时，只会执行 `<no_reply>`。

>### `<delay_reply>` 重触发机制
>1. LLM 输出 `<delay_reply_{秒数}>` → 当前不回复
>2. {秒数}秒后系统重新调用 LLM（传入最近 30 轮对话历史及当前会话人格设定，提示 LLM 根据最后一条用户消息回复）
>3. 新回复再次扫描标签（最多递归 3 层防止死循环；若命中嵌套标签，按优先级处理）
>4. 最终无标签文本发送给用户

## 配置项

### 功能权限

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `feature_mode` | select | `none` | `none`=全员可用 / `whitelist`=仅名单内 / `blacklist`=名单内禁用 |
| `feature_uid_list` | text | (空) | 每行一个完整用户 ID 或会话标识（UMO），仅精确匹配 |

### 回复提示（可单独配置开关）

| 场景 | 开关配置 | 文案配置 | 默认文案 | 占位符 |
|------|---------|---------|----------|--------|
| 拒绝本次回复 | `no_reply_enabled` | `no_reply_text` | `你已被拒绝回复` | — |
| 会话被拒绝回复 | `no_reply_time_enabled` | `no_reply_time_text` | `该会话已被拒绝回复，{time}s后恢复` | `{time}` |
| 指定用户被临时拒绝回复 | `no_reply_user_enabled` | `no_reply_user_text` | `你已被拒绝回复，{time}s后恢复` | `{time}`, `{target}` |
| 延迟回复 | `delay_reply_enabled` | `delay_reply_text` | `将在{time}s后回复你` | `{time}` |

- 所有开关默认 **关闭**，开启后自动**引用触发消息**并发送预设文案
- `{time}` 替换为实际秒数
- `{target}` 替换为用户昵称（通过 `event.get_sender_name()` 获取）

### 上限控制

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_delay_seconds` | int | `3600` | LLM 每次延迟回复的最大秒数 |
| `max_mute_minutes` | int | `1440` | LLM 每次设置拒绝回复的最大分钟数 |

## 用户指令

| 指令 | 参数 | 功能 |
|------|------|------|
| `/refuse_status` | — | 查看当前会话的功能权限、活跃拒绝回复状态列表、延迟任务数 |
| `/refuse_cancel` | — | 取消匹配当前会话的所有拒绝回复状态，终止所有延迟任务 |

## 工作原理

```
LLM 生成回复（可能包含标签）
  │
  ▼
on_decorating_result()
  ├─ 正则扫描 4 种标签
  ├─ 按优先级选取一个生效（no_reply_user > no_reply_time > no_reply > delay_reply）
  │   ├─ no_reply       → 仅标记，不设置拒绝回复状态
  │   ├─ no_reply_time  → _mute_state[umo] = {until: now+N, ...}
  │   ├─ no_reply_user  → _mute_state[target_uid] = {until: now+N, ...}（target 为空则忽略）
  │   └─ delay_reply    → asyncio.create_task(_retrigger_llm(...))
  ├─ 根据配置构建回复链 [Comp.Reply(引用原消息), Comp.Plain(文案)]
  └─ 未开启提示 → 清空链，不作回复
  │
  ▼
发送（或不作回复）
```
