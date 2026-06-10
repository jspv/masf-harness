"""A tiny FastMCP server for the MCP-status gate test: a tool that logs + reports progress."""
import anyio
from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("statusfixture")


@mcp.tool()
async def slow(n: int, ctx: Context) -> str:
    """Process n items, logging and reporting progress as it goes."""
    for i in range(n):
        await ctx.info(f"processing item {i + 1}/{n}")
        await ctx.report_progress(i + 1, n, f"step {i + 1}")
        await anyio.sleep(0.02)
    return f"done: {n} items"


if __name__ == "__main__":
    mcp.run()
