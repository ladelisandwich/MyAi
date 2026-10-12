"""Safe arithmetic evaluator for the calculator tool (no eval/exec, bounded work)."""

from __future__ import annotations

import ast
import math
import operator

MAX_EXPR_LEN = 500
MAX_POW_EXPONENT = 1000
MAX_ABS_RESULT = 1e300

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin,
          "acos": math.acos, "atan": math.atan, "log": math.log, "log10": math.log10, "log2": math.log2,
          "exp": math.exp, "abs": abs, "round": round, "min": min, "max": max, "floor": math.floor,
          "ceil": math.ceil, "factorial": math.factorial, "radians": math.radians, "degrees": math.degrees}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


class CalcError(ValueError):
    pass


def _check(v: float | int) -> float | int:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        raise CalcError("result is not a finite number")
    if abs(v) > MAX_ABS_RESULT:
        raise CalcError("result is too large")
    return v


def _eval(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_POW_EXPONENT:
            raise CalcError("exponent too large")
        try:
            return _check(_BIN[type(node.op)](left, right))
        except ZeroDivisionError as exc:
            raise CalcError("division by zero") from exc
        except OverflowError as exc:
            raise CalcError("result is too large") from exc
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        args = [_eval(a) for a in node.args]
        if node.func.id == "factorial" and (len(args) != 1 or args[0] > 170):
            raise CalcError("factorial argument too large")
        try:
            return _check(_FUNCS[node.func.id](*args))
        except (ValueError, TypeError, OverflowError) as exc:
            raise CalcError(f"{node.func.id}: {exc}") from exc
    raise CalcError(f"unsupported expression element: {type(node).__name__}")


def safe_eval(expr: str) -> float | int:
    expr = expr.strip().replace("^", "**").replace("\u00d7", "*").replace("\u00f7", "/")
    if not expr or len(expr) > MAX_EXPR_LEN:
        raise CalcError("expression is empty or too long")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise CalcError(f"invalid expression: {exc.msg}") from exc
    return _eval(tree)
