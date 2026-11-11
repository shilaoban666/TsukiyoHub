"""内置技能：``calculator`` —— 安全四则运算。

为什么不用 ``eval``
------------------
模型可能生成 ``__import__('os').system(...)``、``().__class__.__bases__`` 这类东西；
只要有一处漏判就是远程代码执行。这里改成 **AST 白名单**：先把表达式解析成语法树，
只允许「数字常量 + 加减乘除/整除/取余/乘方 + 正负号 + 括号」，其它节点一律拒绝，
连函数调用和属性访问的入口都不存在。这样即使模型被提示词注入，也做不了计算以外的事。

这也是「无副作用技能」的样板：不读网络、不读文件、不写上下文，方便单独测试。
"""

from __future__ import annotations

import ast
import math
from typing import Any

from tsukiyohub.kernel.skills.base import BaseSkill, ToolContext
from tsukiyohub.logging_setup import get_logger

logger = get_logger(__name__)

#: 允许的二元运算。刻意不含位运算与矩阵运算——没有业务需要，缩小攻击面。
_ALLOWED_BINOPS: tuple[type[ast.operator], ...] = (
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
)
_ALLOWED_UNARYOPS: tuple[type[ast.unaryop], ...] = (ast.UAdd, ast.USub)

MAX_EXPRESSION = 200      # 表达式长度上限，挡住「几千个数字相加」式的资源消耗
MAX_EXPONENT = 64         # 指数上限
MAX_BASE = 1e50           # 底数上限
MAX_ABS = 1e100           # 结果上限（再大就换成科学计数法提示，不返回 inf）


def _format(value: float) -> str:
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer() and abs(value) < 1e15):
        return str(int(value))
    return f"{value:.12g}"


def evaluate(expression: str) -> float:
    """计算四则运算表达式；不合法时抛 :class:`ValueError`（调用方转成给模型的提示）。"""
    text = (expression or "").strip()
    if not text:
        raise ValueError("表达式为空")
    if len(text) > MAX_EXPRESSION:
        raise ValueError(f"表达式太长（最多 {MAX_EXPRESSION} 个字符）")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise ValueError("表达式语法不正确") from exc

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, int | float):
                raise ValueError("只支持数字")
            value = float(node.value)
            if not math.isfinite(value):
                raise ValueError("数字超出可计算范围")
            return value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARYOPS):
            operand = walk(node.operand)
            return -operand if isinstance(node.op, ast.USub) else operand
        if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
            left, right = walk(node.left), walk(node.right)
            if abs(left) > MAX_BASE or abs(right) > MAX_BASE:
                raise ValueError("数字超出可计算范围")
            if isinstance(node.op, ast.Pow):
                if abs(right) > MAX_EXPONENT:
                    raise ValueError(f"指数太大（最多 {MAX_EXPONENT}）")
                if abs(left) > 1 and abs(right) * math.log10(abs(left)) > 100:
                    raise ValueError("结果太大，请缩小数字")
            try:
                if isinstance(node.op, ast.Add):
                    result = left + right
                elif isinstance(node.op, ast.Sub):
                    result = left - right
                elif isinstance(node.op, ast.Mult):
                    result = left * right
                elif isinstance(node.op, ast.Div):
                    result = left / right
                elif isinstance(node.op, ast.FloorDiv):
                    result = float(left // right)
                elif isinstance(node.op, ast.Mod):
                    result = math.fmod(left, right)
                else:  # ast.Pow
                    result = float(left**right)
            except ZeroDivisionError as exc:
                raise ValueError("除数不能为 0") from exc
            except (OverflowError, ValueError) as exc:
                raise ValueError("数字超出可计算范围") from exc
            return result
        raise ValueError("表达式里只能有数字和 + - * / // % **，不能包含函数调用、变量或属性")

    value = walk(tree)
    if not math.isfinite(value) or abs(value) > MAX_ABS:
        raise ValueError("结果超出可计算范围")
    return value


class CalculatorTool(BaseSkill):
    """``calculator(expression)``：精确到浮点的四则运算。"""

    name = "calculator"
    title = "计算器"
    kind = "tool"
    source = "builtin"
    description = (
        "做加减乘除、整除、取余、乘方等算术计算。"
        "需要精确数值时用它，不要心算；expression 必须是纯算式（例如 (120+30)*0.8），"
        "不能包含变量、函数或单位。"
    )
    rules = "- 涉及数字计算（金额、比例、日期差换算等）时调用 calculator，不要自己心算。"
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "纯四则运算表达式，例如 (120+30)*0.8 或 2**10",
            }
        },
        "required": ["expression"],
    }

    def __init__(self, settings: Any = None) -> None:
        # settings 可选：保持与其它内置技能一致的装配习惯，本技能不需要任何配置
        self.settings = settings

    def call(self, args: dict[str, Any], ctx: ToolContext) -> str:
        expression = str(args.get("expression") or args.get("expr") or "").strip()
        try:
            value = evaluate(expression)
        except ValueError as exc:
            return f"【计算器】{exc}。请改写成一个纯算式再试。"
        except Exception as exc:  # noqa: BLE001 - 任何解析异常都不能让整轮问答失败
            logger.warning("计算器处理失败（%r）：%s", expression, exc)
            return "【计算器】这个表达式算不了，请改写成一个纯算式。"
        result = _format(value)
        ctx.records.append({"tool": self.name, "args": {"expression": expression[:120]}, "result": result})
        return f"{expression} = {result}"


__all__ = ["CalculatorTool", "evaluate", "MAX_EXPRESSION", "MAX_EXPONENT"]