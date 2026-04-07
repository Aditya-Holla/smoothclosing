"""CLI chat interface for the SmoothClosing orchestrator."""

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root so ANTHROPIC_API_KEY and all credentials are available
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    TextBlock,
)
from agents.orchestrator import build_options


async def main():
    print("=" * 55)
    print("  SmoothClosing Acquisitions Assistant")
    print("  Type your request. Type 'quit' to exit.")
    print("=" * 55)
    print()

    session_id = None

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye.")
            break

        opts = build_options(resume_session_id=session_id)

        try:
            async for message in query(prompt=user_input, options=opts):
                # Capture session ID for multi-turn continuity
                if isinstance(message, ResultMessage):
                    session_id = getattr(message, "session_id", session_id)

                    if message.subtype == "success":
                        if message.result:
                            print(f"\nAssistant: {message.result}")
                    elif message.subtype == "error_max_turns":
                        print("\n[Hit turn limit. Try a simpler request or continue.]")
                    else:
                        print(f"\n[Error: {message.subtype}]")

                    cost = getattr(message, "total_cost_usd", None)
                    turns = getattr(message, "num_turns", None)
                    if cost is not None:
                        print(f"  [Cost: ${cost:.4f} | Turns: {turns}]")

                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            print(f"\nAssistant: {block.text}")

        except Exception as e:
            print(f"\n[Error: {e}]")

        print()


def run():
    """Entry point for python -m agents.cli"""
    asyncio.run(main())


if __name__ == "__main__":
    run()
