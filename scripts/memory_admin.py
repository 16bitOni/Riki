"""Memory maintenance.   python -m scripts.memory_admin <command>

  health                       is every memory searchable with the current embedding model?
  reembed [--limit N]          compute missing/old-model vectors (resumable)
  reextract [--since YYYY-MM-DD] [--limit N] [--dry-run]
                               replay archived turns through the extraction prompt
  eval evals/cases.json        score retrieval on a set of questions
"""

import argparse
import asyncio
import json
from datetime import date

from app.db import assert_embedding_dimension, engine, get_or_create_user
from app.memory.evaluation import load_cases, run_eval
from app.memory.maintenance import memory_health, reembed_stale, reextract_archive


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health")
    reembed = commands.add_parser("reembed")
    reembed.add_argument("--limit", type=int)
    reextract = commands.add_parser("reextract")
    reextract.add_argument("--since", type=date.fromisoformat)
    reextract.add_argument("--limit", type=int)
    reextract.add_argument("--dry-run", action="store_true")
    evaluate = commands.add_parser("eval")
    evaluate.add_argument("cases")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    await assert_embedding_dimension()
    user = await get_or_create_user()
    if args.command == "health":
        result = await memory_health(user.id)
    elif args.command == "reembed":
        result = await reembed_stale(user.id, args.limit)
    elif args.command == "reextract":
        result = await reextract_archive(user, args.since, args.limit, args.dry_run)
    else:
        result = await run_eval(user, load_cases(args.cases))
    print(json.dumps(result, indent=2, default=str))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
