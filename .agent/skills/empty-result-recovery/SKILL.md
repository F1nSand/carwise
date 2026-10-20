---
name: empty-result-recovery
description: 结构化查询无结果或条件不受支持时用于安全恢复。
---

# 空结果恢复

先查看 reason_code、observed、data_capabilities 和 relaxation_candidates。区分真正无匹配、样本门槛过高和 unsupported_filter；一次只放宽可解释的条件，并重新调用工具得到新证据。

若移除条件会改变用户要求的范围，先 ask_user 取得明确选择。不要把缺失字段改解释为某种类别，不要把空结果或截断行转换成总体统计。没有有效证据时只说明当前限制和可选下一步。

适用：工具返回 empty 或 unsupported_filter。
不适用：用自由文本推测缺失的统计结果，或把旧条件下的证据复用于新条件。
