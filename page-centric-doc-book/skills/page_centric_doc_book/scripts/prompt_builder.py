"""构造统一提示词。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import TaskRecord


def _normalize_value(value: Any) -> Any:
    primitive_types = (str, int, float, bool)

    if value is None or isinstance(value, primitive_types):
        return value

    if isinstance(value, Path):
        return value.as_posix()

    if isinstance(value, Mapping):
        return {str(key): _normalize_value(val) for key, val in value.items()}

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize_value(item) for item in value]

    if isinstance(value, (set, frozenset)):
        normalized = sorted((_normalize_value(item) for item in value), key=lambda item: str(item))
        return normalized

    return str(value)


class PromptBuilder:
    """封装固定写作约束与事实 JSON。"""

    LEAF_REVIEW_CHECK_ITEMS = [
        "页面说明",
        "入口条件",
        "操作步骤",
        "全部接口",
        "请求字段 / 响应字段",
        "涉及表 / 字段",
        "权限点",
        "异常分支",
        "流程图",
        "关联页面",
        "业务逻辑说明",
    ]
    LEAF_FINALIZE_SECTION_TITLES = [f"## {item}" for item in LEAF_REVIEW_CHECK_ITEMS]

    def build(self, task: TaskRecord, context: Mapping[str, Any]) -> str:
        facts = {str(key): _normalize_value(val) for key, val in context.items()}
        if task.kind == "page_leaf_review":
            return self._build_leaf_review_prompt(task, context, facts)
        if task.kind == "page_leaf_enrich":
            return self._build_leaf_enrich_prompt(task, context, facts)
        if task.kind == "page_leaf_finalize":
            return self._build_leaf_finalize_prompt(task, context, facts)

        return self._build_default_prompt(task, facts)

    def _badge(self, label: str, value: Any) -> str:
        normalized = str(value or "").strip()
        return f"{label}: {normalized or '未知'}"

    def _build_default_prompt(self, task: TaskRecord, facts: Mapping[str, Any]) -> str:
        lines = [
            "任务信息：",
            f"任务 ID: {task.task_id}",
            f"任务类型: {task.kind}",
            f"目标输出: {task.output}",
            "",
            "写作约束：",
            "1. 只能基于提供事实写作",
            "2. 如果信息不足，必须明确写“未在当前静态扫描中确认”。",
            "",
            "事实 JSON：",
            json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True),
        ]
        return "\n".join(lines)

    def _build_leaf_review_prompt(
        self,
        task: TaskRecord,
        context: Mapping[str, Any],
        facts: Mapping[str, Any],
    ) -> str:
        round_number = context.get("page_leaf_round") or context.get("page_leaf_review_round") or 1
        record = context.get("page_leaf_gaps") or []
        lines = [
            "任务信息：",
            f"任务 ID: {task.task_id}",
            f"任务类型: {task.kind}",
            f"目标输出: {task.output}",
            "",
            "复审轮次：",
            f"- 第{round_number}轮",
            "",
            "当前缺口：",
        ]
        if record:
            lines.extend(f"- {entry}" for entry in record)
        else:
            lines.append("- 当前暂无缺口")
        lines.extend(
            [
                "",
                "复审检查项（请逐条核对，仅在缺口项下补全）：",
            ]
        )
        lines.extend(f"{index + 1}. {item}" for index, item in enumerate(self.LEAF_REVIEW_CHECK_ITEMS))
        lines.extend(
            [
                "",
                "写作约束：",
                "1. 只能基于提供事实写作",
                "2. 如果信息不足，必须明确写“未在当前静态扫描中确认”。",
                "3. 只能输出缺口清单，禁止生成任何正文或建议段落。",
                "",
                "事实 JSON：",
                json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True),
            ]
        )
        return "\n".join(lines)

    def _build_leaf_enrich_prompt(
        self,
        task: TaskRecord,
        context: Mapping[str, Any],
        facts: Mapping[str, Any],
    ) -> str:
        round_number = context.get("page_leaf_round") or context.get("page_leaf_review_round") or 1
        gaps = context.get("page_leaf_gaps") or []
        lines = [
            "任务信息：",
            f"任务 ID: {task.task_id}",
            f"任务类型: {task.kind}",
            f"目标输出: {task.output}",
            "",
            f"补链轮次：第{round_number}轮",
            "",
            "当前需补充的缺口：",
        ]
        if gaps:
            lines.extend(f"- {gap}" for gap in gaps)
        else:
            lines.append("- 暂无已知缺口，用当前证据支撑已有内容。")
        lines.extend(
            [
                "",
                "补链要求：",
                "1. 仅产出能引入新证据的段落，避免复述已有描述。",
                "2. 每一条内容必须围绕缺口展开并注明出处或路径。",
                "",
                "写作约束：",
                "1. 只能基于提供事实写作",
                "2. 如果信息不足，必须明确写“未在当前静态扫描中确认”。",
                "",
                "事实 JSON：",
                json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True),
            ]
        )
        return "\n".join(lines)

    def _build_leaf_finalize_prompt(
        self,
        task: TaskRecord,
        context: Mapping[str, Any],
        facts: Mapping[str, Any],
    ) -> str:
        page_status = context.get("page_status") or "draft"
        remaining_gaps = context.get("remaining_gaps") or "- 无"
        lines = [
            "任务信息：",
            f"任务 ID: {task.task_id}",
            f"任务类型: {task.kind}",
            f"目标输出: {task.output}",
            "",
            "终稿状态：",
            f"- 当前状态：{page_status}",
            "- 固定 11 节结构",
            "- 不得增删章节，不得改名，不得调整顺序",
            "- 每个章节都必须输出；如果信息不足，明确写“未在当前静态扫描中确认”。",
            "- 只输出最终 Markdown 正文，不要输出解释、前言、结语或额外提示。",
            "",
            "剩余缺口：",
            str(remaining_gaps),
            "",
            "最终文档必须严格包含以下章节：",
        ]
        lines.extend(self.LEAF_FINALIZE_SECTION_TITLES)
        lines.extend(
            [
                "",
                "写作要求：",
                "1. 页面说明：概括页面定位、用户目标和页面产出。",
                "2. 入口条件：说明菜单入口、前置状态、登录/权限或上下文条件。",
                "3. 操作步骤：按用户操作顺序描述。",
                "4. 全部接口：覆盖页面实际调用的全部接口。",
                "5. 请求字段 / 响应字段：逐接口说明已确认字段；未确认部分必须显式说明。",
                "6. 涉及表 / 字段：覆盖数据表、数据库字段、Go Model 或映射关系。",
                "7. 权限点：列出权限标识、角色或鉴权要求。",
                "8. 异常分支：列出失败、空态、边界条件或错误提示。",
                "9. 流程图：输出 Mermaid。",
                "10. 关联页面：列出上下游页面与关系。",
                "11. 业务逻辑说明：总结核心规则、状态流、业务约束。",
                "",
                "事实 JSON：",
                json.dumps(facts, ensure_ascii=False, indent=2, sort_keys=True),
            ]
        )
        return "\n".join(lines)
