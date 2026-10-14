"""Web search through Tavily (free tier is plenty for personal use)."""

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.config import settings
from app.core.privacy import scrub, tag_untrusted


MAX_IMAGES = 3


@tool
async def web_search(query: str) -> str:
    """Search the web for current information. Returns a short answer and the top sources."""
    if not settings.tavily_api_key:
        return "Web search is not set up: TAVILY_API_KEY is missing in .env."
    safe_query = scrub(query)
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
            json={"query": safe_query, "max_results": 5, "include_answer": True},
        )
    if response.status_code != 200:
        return f"Web search failed ({response.status_code})."
    data = response.json()
    answer = data.get("answer") or ""
    results = data.get("results") or []
    if not results and not answer:
        return f"No results found for: {query}"
    sources = "\n".join(f"- {r['title']} ({r['url']}): {r['content'][:300]}" for r in results)
    return tag_untrusted(f"{answer}\n\nSources:\n{sources}".strip())


@tool
async def image_search(query: str, *, config: RunnableConfig) -> str:
    """Find pictures of something (a place, object, person, animal...). The pictures are sent to the user
    automatically with the reply; you only need to say briefly what they show."""
    if not settings.tavily_api_key:
        return "Image search is not set up: TAVILY_API_KEY is missing in .env."
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {settings.tavily_api_key}"},
            json={"query": query, "max_results": 3, "include_images": True, "include_image_descriptions": True},
        )
    if response.status_code != 200:
        return f"Image search failed ({response.status_code})."
    found = response.json().get("images", [])
    urls = [i["url"] if isinstance(i, dict) else i for i in found][:MAX_IMAGES]
    if not urls:
        return "No pictures found."
    config["configurable"]["images"].extend(urls)  # chat_service hands these to the front end
    notes = [i.get("description") for i in found[:MAX_IMAGES] if isinstance(i, dict) and i.get("description")]
    return f"Found {len(urls)} pictures; they will be sent with your reply." + ("\n" + "\n".join(f"- {n}" for n in notes) if notes else "")
