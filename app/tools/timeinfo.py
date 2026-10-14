from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.core.timeutil import now_in


@tool
async def get_current_time(*, config: RunnableConfig) -> str:
    """Current date, time and weekday in the user's timezone. Use for any date arithmetic."""
    user = config["configurable"]["user"]
    return now_in(user.timezone).strftime("%A, %Y-%m-%d %H:%M %Z (UTC%z)")
