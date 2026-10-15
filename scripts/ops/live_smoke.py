#!/usr/bin/env python3
"""Plan and run the approval-gated live smoke of Bridge, VirusTotal and Shodan.

Run it inside the stack worker so it uses the production settings and secrets:

    make smoke-live-plan                                  # prints plan + SHA-256, no network
    make smoke-live SMOKE_LIVE_PLAN_SHA256=<sha> SMOKE_LIVE_OPERATOR=<name>

The approval is bound to the exact plan hash; a changed plan needs a new approval.
Exit codes: 0 every probe PASS, 1 a probe failed or was not validated, 2 approval
missing or not matching the plan.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from datetime import UTC, datetime

from cti_app.application.live_smoke import (
    SettingsLiveSmokeExecutor,
    build_live_smoke_plan,
    local_tool_inventory,
    open_live_smoke_clients,
)
from cti_app.application.provider_probes import (
    LiveSmokeApproval,
    LiveSmokeApprovalRequiredError,
    ProviderProbeStatus,
    run_live_smoke,
)
from cti_app.config import get_settings


def _print_plan(plan, *, as_json: bool) -> None:
    payload = {
        "plan_sha256": plan.sha256,
        "tlp": plan.tlp.value,
        "operator_effect": plan.operator_effect,
        "expected_total_cost": plan.expected_total_cost,
        "endpoints": [
            {
                "provider": item.provider,
                "operation": item.operation,
                "request": f"{item.method.upper()} {item.endpoint}",
                "submitted_data": dict(item.submitted_data),
                "effect": item.effect,
                "expected_cost": item.expected_cost,
            }
            for item in plan.endpoints
        ],
    }
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"Plan SHA-256 : {plan.sha256}")
    print(f"Effet        : {plan.operator_effect}")
    print(f"Coût attendu : {plan.expected_total_cost}")
    for item in payload["endpoints"]:
        print(f"- {item['provider']}.{item['operation']}: {item['request']}")
        print(f"    données : {item['submitted_data']}")
        print(f"    effet   : {item['effect']} ({item['expected_cost']})")


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    plan = build_live_smoke_plan(settings)
    if args.plan_only:
        _print_plan(plan, as_json=args.json)
        return 0
    if not args.approve_sha256 or not args.operator:
        _print_plan(plan, as_json=args.json)
        print(
            "\nApprobation requise : relancez avec --approve-sha256 <sha> --operator <nom>"
            " (make smoke-live SMOKE_LIVE_PLAN_SHA256=<sha> SMOKE_LIVE_OPERATOR=<nom>).",
            file=sys.stderr,
        )
        return 2
    approval = LiveSmokeApproval(
        operator_id=args.operator,
        approved_at=datetime.now(UTC),
        plan_sha256=args.approve_sha256.strip().lower(),
    )
    async with AsyncExitStack() as stack:
        clients = await open_live_smoke_clients(settings, stack)
        executor = SettingsLiveSmokeExecutor(
            clients,
            bridge_model=settings.openai_structured_model,
            bridge_turn_timeout_seconds=min(settings.openai_bridge_wait_timeout_seconds, 600.0),
        )
        try:
            results = await run_live_smoke(plan, operator_approval=approval, executor=executor)
        except LiveSmokeApprovalRequiredError as exc:
            print(f"Refusé : {exc}", file=sys.stderr)
            return 2
    inventory = local_tool_inventory()
    if args.json:
        print(
            json.dumps(
                {
                    "plan_sha256": plan.sha256,
                    "operator": args.operator,
                    "results": [
                        {
                            "provider": item.provider,
                            "operation": item.operation,
                            "status": item.status.value,
                            "http_status": item.http_status,
                            "duration_ms": round(item.duration_ms, 1),
                            "error_code": item.error_code,
                            "summary": item.summary,
                        }
                        for item in results
                    ],
                    "local_tools": inventory,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(f"Smoke live — plan {plan.sha256[:12]}… approuvé par {args.operator}")
        for item in results:
            print(
                f"[{item.status.value:13}] {item.provider}.{item.operation}"
                f" http={item.http_status} {item.duration_ms:.0f} ms"
                f"{' code=' + item.error_code if item.error_code else ''} — {item.summary}"
            )
        present = ", ".join(name for name, ok in inventory.items() if ok) or "aucun"
        missing = ", ".join(name for name, ok in inventory.items() if not ok) or "aucun"
        print(f"Outils locaux (informatif) : présents={present} ; absents={missing}")
    ok = all(item.status is ProviderProbeStatus.PASS for item in results)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan-only", action="store_true", help="Affiche le plan et son SHA-256")
    parser.add_argument("--approve-sha256", default="", help="SHA-256 exact du plan approuvé")
    parser.add_argument("--operator", default="", help="Identité de l'opérateur qui approuve")
    parser.add_argument("--json", action="store_true")
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
