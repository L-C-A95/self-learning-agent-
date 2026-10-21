import asyncio
from deepdive.search.searxng import SearxNGClient

async def main():
    client = SearxNGClient()
    print("Connecting to:", client.base_url)
    try:
        results = await client.search("test")
        print("Results count:", len(results))
        for r in results[:3]:
            print("-", r.title)
    except Exception as e:
        print("ERROR:", type(e).__name__, e)
    finally:
        await client.aclose()

asyncio.run(main())