"""Simulate an inbound call WITHOUT the phone segment.

Given a shop + a caller number, this resolves the caller against the real DB,
assembles the exact session prompt the agent would receive, and shows the data
its tools would return — so we can verify "right data for the right caller"
before any telephony/OpenAI wiring.

Usage:
    python scripts/simulate_call.py <shop_id> <caller_number>
    python scripts/simulate_call.py 5e0b3ecf-c85f-478f-9369-859c419e7df0 "+39 348 1234567"
"""
from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from booking_engine.config import Settings
from booking_engine.db import connection
from booking_engine.db import voice_tool_queries as tools
from booking_engine.db.voice_config_queries import get_config, get_policy
from booking_engine.services.identity_resolver import resolve_caller
from booking_engine.services.phone_normalize import digits_only
from booking_engine.services.prompt_assembler import assemble_session_prompt


async def simulate(shop_id: UUID, caller: str) -> None:
    config = await get_config(shop_id)
    policy = await get_policy()
    if not config or not policy:
        print(f"!! shop {shop_id} has no voice config/policy — cannot run")
        return

    resolution = await resolve_caller(shop_id=shop_id, caller_phone=caller)
    assembled = await assemble_session_prompt(
        config=config, policy=policy, resolution=resolution,
    )

    print("=" * 70)
    print(f"CALLER: {caller}   (digits: {digits_only(caller) or 'anonymous'})")
    print(f"SHOP:   {shop_id}  display_name={config.get('display_name')!r}")
    print("-" * 70)
    match = resolution.unique_match
    if resolution.is_anonymous:
        print("RESOLUTION: anonymous caller")
    elif match:
        print(f"RESOLUTION: known -> {match.first_name} "
              f"(tags={match.notes_tags}, verified={match.verified})")
    elif len(resolution.matches) > 1:
        print(f"RESOLUTION: ambiguous -> {len(resolution.matches)} customers")
    else:
        print("RESOLUTION: new/unknown caller")

    # Show the caller-context line the model gets
    for line in assembled.prompt.splitlines():
        if line.startswith(("SEI L'ASSISTENTE", "Il cliente", "Il chiamante")):
            print("PROMPT:", line)
    print(f"TOOLS REGISTERED: {len(assembled.tools)}  voice={assembled.voice}")

    # What the tools return for THIS caller, from the real DB
    services = await tools.list_services(shop_id=shop_id, filter_q=None)
    print(f"get_services -> {len(services)} services, e.g. "
          f"{[s['name'] for s in services[:4]]}")
    if match:
        nb = await tools.get_next_booking_for_customer(
            shop_id=shop_id, customer_id=match.customer_id)
        print(f"get_booking(caller) -> "
              f"{nb['service_name'] + ' @ ' + str(nb['start_time']) if nb else 'no upcoming booking'}")
    print()


async def main() -> None:
    shop_id = UUID(sys.argv[1])
    caller = sys.argv[2] if len(sys.argv) > 2 else ""
    await connection.init_connection(Settings())
    try:
        await simulate(shop_id, caller)
    finally:
        await connection.close_connection()


if __name__ == "__main__":
    asyncio.run(main())
