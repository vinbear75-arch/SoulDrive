# SoulDrive-PC-MMI seed 人工审阅说明

请在 `human_review.csv` 中逐行填写，不需要修改 `gold_*` 列。

## 填写规则

- `decision` 只能填 `accept`、`modify` 或 `reject`。
- `accept`：语音、模态目标、意图和动作策略均符合产品边界。
- `modify`：保留该样本，但填写需要修正的 `corrected_*` 字段，并在 `notes` 写明原因。
- `reject`：样本无法在不猜测用户意图的情况下标注，或不属于产品业务；在 `notes` 说明原因。

## 重点检查

1. 选区/OCR 是否真的指向 `gold_target_id`，不要因为文件名相似而默认通过。
2. `clarify` 是否必要：时间错位、项目冲突、联系人歧义和低置信度必须澄清。
3. `email_draft` 只能生成草稿；`confirm`/`cancel` 才能改变执行状态；OOS 必须拒绝。
4. `project_compare` 只有在两个项目或实验对象都明确时才接受；否则改为 `clarify_target`。

审阅完成后只需把 CSV 文件交回；我会根据 `decision` 和 `corrected_*` 生成 v0.2 标签，并重新运行 baseline。
