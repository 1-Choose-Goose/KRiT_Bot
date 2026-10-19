"""Controlled WebSocket load scenario for an already authenticated test teacher.

Run only against staging or an explicitly prepared production lesson. The script does not
contain credentials and never requests OTP codes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlparse

import websockets


@dataclass
class Result:
    sent: int
    received: int
    duration: float


async def run_client(
    *,
    client_number: int,
    ws_url: str,
    origin: str,
    cookie_name: str,
    session_token: str,
    barrier: asyncio.Barrier,
    operations_per_client: int,
    expected_operations: int,
) -> Result:
    started = time.perf_counter()
    received_ids: set[str] = set()
    async with websockets.connect(
        ws_url,
        origin=origin,
        additional_headers={"Cookie": f"{cookie_name}={session_token}"},
        max_size=2 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    ) as socket:
        await barrier.wait()
        for index in range(operations_per_client):
            operation_id = f"load-{client_number}-{index}-{uuid.uuid4()}"
            await socket.send(
                json.dumps(
                    {
                        "type": "board.operation",
                        "operation": {
                            "operation_id": operation_id,
                            "page_id": "load-test",
                            "kind": "add",
                            "payload": {
                                "object": {
                                    "id": operation_id,
                                    "type": "path",
                                    "points": [{"x": index, "y": client_number}],
                                }
                            },
                        },
                    },
                    separators=(",", ":"),
                )
            )
        async with asyncio.timeout(60):
            while len(received_ids) < expected_operations:
                message = json.loads(await socket.recv())
                if message.get("type") == "board.operation":
                    operation_id = message.get("operation", {}).get("operation_id", "")
                    if operation_id.startswith("load-"):
                        received_ids.add(operation_id)
    return Result(operations_per_client, len(received_ids), time.perf_counter() - started)


async def run(args: argparse.Namespace) -> None:
    parsed = urlparse(args.api_base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    api_path = parsed.path.rstrip("/")
    ws_url = f"{scheme}://{parsed.netloc}{api_path}/rooms/{args.room_id}/ws"
    expected = args.participants * args.operations_per_client
    barrier = asyncio.Barrier(args.participants)
    results = await asyncio.gather(
        *(
            run_client(
                client_number=index,
                ws_url=ws_url,
                origin=args.origin,
                cookie_name=args.cookie_name,
                session_token=args.session_token,
                barrier=barrier,
                operations_per_client=args.operations_per_client,
                expected_operations=expected,
            )
            for index in range(args.participants)
        )
    )
    durations = sorted(item.duration for item in results)
    summary = {
        "participants": args.participants,
        "operations": sum(item.sent for item in results),
        "received_per_client_min": min(item.received for item in results),
        "duration_seconds_p50": round(statistics.median(durations), 3),
        "duration_seconds_p95": round(durations[max(0, int(len(durations) * 0.95) - 1)], 3),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["received_per_client_min"] != expected:
        raise SystemExit("Not every client received every operation")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", required=True, help="https://host/krit-api/api/v1/online")
    parser.add_argument("--origin", required=True, help="Allowed site origin")
    parser.add_argument("--room-id", required=True, type=int)
    parser.add_argument(
        "--session-token", required=True, help="Temporary test session cookie value"
    )
    parser.add_argument("--cookie-name", default="krit_online_session")
    parser.add_argument("--participants", required=True, type=int, choices=(10, 20, 30, 50))
    parser.add_argument("--operations-per-client", type=int, default=5)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
