"""Load test for the public chat flow (prd.md 10 / 16: "20 simultaneous chats").

Each virtual visitor does what the widget does: GET config, POST session, then N chat messages.
Run it against STAGING. --origin must be one of the bot's allowed domains (it is sent as a header,
so the test can run from any machine). Note:

  * All requests come from this machine's single IP, so the per-IP widget rate limits
    (20 messages / 10 min per IP per bot, 10 new sessions / 10 min per IP) will block most of the
    test. Raise them temporarily in Admin -> Settings before running, and restore them after.
  * Messages count toward the bot owner's monthly quota. Use a dedicated test customer/bot.

Usage (needs httpx: `pip install httpx`, already in ai-service/.venv):
  python scripts/loadtest.py --base https://app.example.com --key pk_xxx --origin https://shop.example.com \
      --visitors 20 --messages 3
"""

import argparse
import asyncio
import statistics
import time
import uuid

import httpx

QUESTIONS = [
    "What are your opening hours?",
    "Do you deliver?",
    "How can I contact you?",
    "Do you have gluten free options?",
    "Where are you located?",
]


async def visitor(client: httpx.AsyncClient, args, index: int, results: list) -> None:
    headers = {"Origin": args.origin}
    session_id = str(uuid.uuid4())

    def record(kind: str, started: float, response: httpx.Response | None, error: str | None = None) -> None:
        results.append({"kind": kind, "ms": (time.perf_counter() - started) * 1000,
                        "status": response.status_code if response is not None else None, "error": error})

    try:
        t = time.perf_counter()
        r = await client.get(f"{args.base}/api/widget/config", params={"key": args.key}, headers=headers)
        record("config", t, r)

        t = time.perf_counter()
        r = await client.post(f"{args.base}/api/widget/session", json={"key": args.key, "session_id": session_id}, headers=headers)
        record("session", t, r)
        if r.status_code != 200:
            return
        token = r.json()["token"]

        for n in range(args.messages):
            t = time.perf_counter()
            r = await client.post(
                f"{args.base}/api/widget/chat",
                json={"key": args.key, "session_id": session_id, "message": QUESTIONS[(index + n) % len(QUESTIONS)],
                      "page_url": f"{args.origin}/loadtest"},
                headers={**headers, "Authorization": f"Bearer {token}"},
            )
            record("chat", t, r)
    except httpx.HTTPError as exc:
        results.append({"kind": "error", "ms": 0, "status": None, "error": type(exc).__name__})


def summarize(results: list, wall: float) -> int:
    print(f"\nTotal wall time: {wall:.1f}s, requests: {len(results)}")
    failed = 0
    chat_p95 = None
    for kind in ("config", "session", "chat"):
        rows = [r for r in results if r["kind"] == kind]
        if not rows:
            continue
        ok = [r["ms"] for r in rows if r["status"] == 200]
        bad = [r for r in rows if r["status"] != 200]
        failed += len(bad)
        line = f"{kind:8} n={len(rows):4}  ok={len(ok):4}  failed={len(bad):3}"
        if ok:
            ok_sorted = sorted(ok)
            p95 = ok_sorted[max(0, int(len(ok_sorted) * 0.95) - 1)]
            line += f"  p50={statistics.median(ok):7.0f}ms  p95={p95:7.0f}ms  max={max(ok):7.0f}ms"
            if kind == "chat":
                chat_p95 = p95
        print(line)
        statuses = {}
        for r in bad:
            statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        if statuses:
            print(f"         failure statuses: {statuses}" + ("  (429 = rate limited: raise limits in Admin -> Settings)" if 429 in statuses else ""))
    errors = [r for r in results if r["kind"] == "error"]
    if errors:
        print(f"transport errors: {len(errors)} ({', '.join(sorted({e['error'] for e in errors}))})")
    slow = chat_p95 is None or chat_p95 >= 8000
    target = "PASS" if failed == 0 and not errors and not slow else "FAIL"
    print(f"\nResult: {target} (prd.md 10 targets: no errors at 20 simultaneous chats, chat p95 < 8s)"
          + ("  <- chat p95 too slow" if slow and failed == 0 and not errors else ""))
    return 0 if target == "PASS" else 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="App base URL, e.g. https://app.example.com")
    parser.add_argument("--key", required=True, help="Bot public key (pk_...)")
    parser.add_argument("--origin", required=True, help="An allowed origin for the bot, e.g. https://shop.example.com")
    parser.add_argument("--visitors", type=int, default=20)
    parser.add_argument("--messages", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()

    results: list = []
    limits = httpx.Limits(max_connections=args.visitors * 2)
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        started = time.perf_counter()
        await asyncio.gather(*(visitor(client, args, i, results) for i in range(args.visitors)))
        wall = time.perf_counter() - started
    return summarize(results, wall)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
