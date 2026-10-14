"""Run the Desktop Operator from a terminal, with approvals answered right here.

    python -m scripts.desktop_operator "Prepare my development environment for the memory-agent project"

Needs OPERATOR_ROOTS (folders it may touch) and the extras:  pip install -r requirements-desktop.txt
Stop it any time with Ctrl+C, or by slamming the mouse into a screen corner.
"""

import argparse
import asyncio
import json

from app.config import settings
from app.desktop.agent import Approver, DesktopOperator
from app.desktop.computer import DesktopComputer
from app.desktop.policy import Policy
from app.desktop.schemas import ApprovalRequest, StepRecord


class TerminalApprover(Approver):
    async def approve(self, request: ApprovalRequest) -> bool:
        print(f"\n--- APPROVAL NEEDED ---\n{request.summary}\nWhy asked: {request.reason}\n{request.detail}\n")
        answer = await asyncio.to_thread(input, "Allow? [y/N] ")
        return answer.strip().lower() in ("y", "yes")


def show(record: StepRecord) -> None:
    action = json.dumps(record.action, ensure_ascii=False)
    print(f"[{record.number}] {record.verdict:8} {action[:160]}\n     {record.result[:200]!r}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("goal")
    goal = parser.parse_args().goal

    if not settings.operator_root_paths:
        raise SystemExit("Set OPERATOR_ROOTS in .env to the folder(s) the operator may use.")
    policy = Policy(settings.operator_root_paths, settings.operator_auto_approve_edits)
    operator = DesktopOperator(DesktopComputer(settings.operator_screenshot_width), policy, TerminalApprover(), on_step=show)
    try:
        report = await operator.run(goal)
    except KeyboardInterrupt:
        operator.cancel.set()
        raise
    print("\n=== REPORT ===")
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
