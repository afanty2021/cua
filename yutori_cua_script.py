"""Minimal Yutori n1 + CUA cloudv2 script."""

import asyncio
import json
import logging
import os

import dotenv

dotenv.load_dotenv()

from agent import ComputerAgent
from computer import Computer

CUA_API_KEY = os.environ["CUA_API_KEY"]
YUTORI_API_KEY = os.environ["YUTORI_API_KEY"]

TASK = "open firefox and go to google.com"


async def main():
    async with Computer(
        os_type="linux",
        provider_type="cloudv2",
        name="cunning-bluejay",
        api_key=CUA_API_KEY,
    ) as computer:
        agent = ComputerAgent(
            model="yutori/n1",
            tools=[computer],
            api_key=YUTORI_API_KEY,
            trajectory_dir="trajectories",
            verbosity=logging.INFO,
        )

        history = [{"role": "user", "content": TASK}]
        print(f"Task: {TASK}")

        async for result in agent.run(history, stream=False):
            history += result["output"]
            for item in result["output"]:
                print(json.dumps(item, indent=2, default=str)[:500])


asyncio.run(main())
