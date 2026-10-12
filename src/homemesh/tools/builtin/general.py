"""General-purpose coordinator tools with no side effects."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from homemesh.tools.base import ToolContext, ToolDefinition
from homemesh.tools.safe_eval import safe_eval


class CalculatorInput(BaseModel):
    expression: str = Field(description="Arithmetic expression, e.g. '(3.5 * 4) / sqrt(2)'", max_length=500)


async def _calculator(ctx: ToolContext, args: CalculatorInput) -> dict[str, Any]:
    return {"expression": args.expression, "result": safe_eval(args.expression)}


class TimeInput(BaseModel):
    pass


async def _current_time(ctx: ToolContext, args: TimeInput) -> dict[str, Any]:
    now = datetime.now().astimezone()
    return {"local_time": now.isoformat(timespec="seconds"), "timezone": str(now.tzinfo),
            "weekday": now.strftime("%A")}


CALCULATOR = ToolDefinition(
    name="calculator", description="Evaluate an arithmetic expression exactly. Supports + - * / // % ** and "
                                   "sqrt, sin, cos, tan, log, log10, exp, abs, round, min, max, floor, ceil, pi, e.",
    input_model=CalculatorInput, output_description="{expression, result}", handler=_calculator,
    side_effect="none", timeout_s=5, permissions=[],
)
CURRENT_TIME = ToolDefinition(
    name="current_time", description="Get the current local date, time and time zone of the HomeMesh coordinator.",
    input_model=TimeInput, output_description="{local_time, timezone, weekday}", handler=_current_time,
    side_effect="none", timeout_s=5,
)
