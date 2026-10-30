"""Minimal bot: replies 'echo: ...' and reacts with 👀 to every text message.

    SG_API_KEY=... SG_ACCOUNT=+39333... python examples/echo_bot.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from client.sdk import SignalGateway

ACCOUNT = os.environ["SG_ACCOUNT"]


async def main() -> None:
    sg = SignalGateway(os.getenv("SG_URL", "http://localhost:8000"),
                       api_key=os.getenv("SG_API_KEY", ""))
    print(f"Listening as {ACCOUNT} ... Ctrl+C to stop")
    async for event in sg.events(account=ACCOUNT):
        if event.get("is_sync") or not event.get("text"):
            continue  # ignore our own messages sent from other devices
        print(f"{event['source']}: {event['text']}")
        if event.get("group_id"):
            sg.send(ACCOUNT, f"echo: {event['text']}", group_id=event["group_id"])
        elif event.get("source"):
            sg.react(ACCOUNT, "👀", event["source"], event["timestamp"],
                     recipient=event["source"])
            sg.send(ACCOUNT, f"echo: {event['text']}", recipients=[event["source"]])


if __name__ == "__main__":
    asyncio.run(main())
