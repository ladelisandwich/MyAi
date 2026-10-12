"""Coordinator tools that search the user's saved memories and indexed documents (read-only)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from homemesh.tools.base import ToolContext, ToolDefinition


class SearchInput(BaseModel):
    query: str = Field(description="What to look for", min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


async def _search_memory(ctx: ToolContext, args: SearchInput) -> dict[str, Any]:
    if ctx.services is None or ctx.user_id is None:
        raise RuntimeError("memory service unavailable")
    items = await ctx.services.memory.search(ctx.user_id, args.query, limit=args.limit)
    return {"memories": [{"id": m["id"], "content": m["content"], "kind": m["kind"], "source": m["source"],
                          "saved_at": m["created_at"]} for m in items]}


async def _search_knowledge(ctx: ToolContext, args: SearchInput) -> dict[str, Any]:
    if ctx.services is None or ctx.user_id is None:
        raise RuntimeError("knowledge service unavailable")
    hits = await ctx.services.knowledge.search(ctx.user_id, args.query, limit=args.limit)
    return {"results": [{"source": h["source"], "excerpt": h["content"][:800], "score": h["score"]} for h in hits]}


SEARCH_MEMORY = ToolDefinition(
    name="search_memory", description="Search the user's saved memories (facts and preferences they asked you to "
                                      "remember). Returns memory text with when it was saved.",
    input_model=SearchInput, output_description="{memories: [...]}", handler=_search_memory,
    side_effect="read", permissions=["memory.read"], timeout_s=20,
)
SEARCH_KNOWLEDGE = ToolDefinition(
    name="search_knowledge", description="Search the user's indexed document collections and return matching "
                                         "excerpts with their source file references.",
    input_model=SearchInput, output_description="{results: [{source, excerpt, score}]}", handler=_search_knowledge,
    side_effect="read", permissions=["knowledge.read"], timeout_s=60,
)
