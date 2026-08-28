# 术语约定：「模组图片补充生成」

用户口中的「模组图片补充生成」专指**对已生成的模组（.lwpack）事后补配图**的整套逻辑，
与「生成模组时勾选配图」是两条独立路径。

## 触发入口

- 详情页「生成配图」按钮（`module_media_generate` 帧）
- `.forge media <模组名>` 命令（含 `retry` / `status` 子命令）

## 逻辑链（`gateway/module_media.py`）

1. `plan_media_jobs` — 读模组世界卡的条目（NPC/场景/线索/物品的名字），
   让 AI 设计一批配图任务（类型 + 主题 + 提示词），写入 `media-jobs.json`；
   配图主题用世界卡真实条目名（`_worldbook_subject_names`），保证渲染后可绑定回条目。
2. `schedule_pack_media` — 排队 + 启动后台 worker。
3. 后台 worker — **串行渲染**（每张约 25 秒；2026-08-28 起无配额门，满速跑完；
   失败保留提示词可重试，失败后 30 秒退避防 provider 限流连打）。
4. 渲染完成 — 图片写入 `assets/`、manifest `assets` 登记、
   `_bind_card_images` 绑定到世界卡条目（NPC 立绘绑 NPC 条目、线索图绑线索条目）、
   pregen 头像同步认领角色（`_update_claimed_avatars`）。

## 生效边界（用户已认可）

补图绑定写的是 **pack 源文件**；已导入房间的 NPC/线索条目图**不自动同步**——
房间内生效需要重新导入模组（会清当前剧本进度）。pregen 头像除外（认领角色即时同步）。
