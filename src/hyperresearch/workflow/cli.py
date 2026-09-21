"""hr-workflow console script. Not an hpr subcommand."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx
import typer

from hyperresearch.workflow import (
    WorkflowError,
    drain,
    ingest_retry,
    wiki_draft,
)

app = typer.Typer(no_args_is_help=True)


def _vault() -> Any:
    from hyperresearch.core.vault import Vault

    company = os.environ.get("HYPERRESEARCH_SHOSHIN_VAULT")
    if company:
        root = Path(company)
        return Vault(root) if (root / ".hyperresearch").is_dir() else Vault.init(root, name="Shoshin")
    return Vault.discover()


def _gbrain() -> Any:
    from hyperresearch.knowledge.gbrain import GBrainClient, GBrainError

    url = os.environ.get("GBRAIN_MCP_URL") or "https://brain-jarvis-company.tail8ab21.ts.net/mcp"
    bearer = os.environ.get("GBRAIN_SHOSHIN_BEARER") or os.environ.get("GBRAIN_SHOSHIN_CONTENT_BEARER") or ""
    if not bearer:
        raise GBrainError("unconfigured")
    return GBrainClient(url, bearer)


@app.command("drain")
def drain_cmd(
    company: str = typer.Option(..., "--company"),
    tier: str = typer.Option("full", "--tier"),
) -> None:
    import asyncio

    from hyperresearch.pipeline.orchestrator import execute_run
    from hyperresearch.runtime.fake import FakeRuntime

    vault = _vault()
    lock_path = vault.root / ".hr-workflow.lock"

    def run_hpr(query: str, run_id: str, resume: bool = False) -> Any:
        return asyncio.run(
            execute_run(
                vault,
                query,
                FakeRuntime(default={"kind": "complete", "args": {}, "reason": "worker"}),
                profile="full" if tier == "full" else "light",
                tag=run_id,
                resume=resume,
                company=company,
            )
        )

    try:
        gbrain = _gbrain()
        pages = gbrain.list_pages(prefix="companies/shoshin/research/queue/")
        queue = pages if isinstance(pages, list) else (pages or {}).get("pages") or []
        paperclip = httpx.Client(timeout=30.0)
        drain(
            company=company,
            tier=tier,
            vault=vault,
            gbrain=gbrain,
            paperclip=paperclip,
            run_hpr=run_hpr,
            lock_path=lock_path,
            queue_pages=queue,
        )
    except WorkflowError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(exc.code) from exc


@app.command("ingest-retry")
def ingest_retry_cmd(
    run_id: str = typer.Argument(...),
    company: str = typer.Option("shoshin", "--company"),
) -> None:
    vault = _vault()
    try:
        ingest_retry(
            httpx.Client(timeout=30.0),
            vault.run_dir(run_id),
            f"companies/shoshin/research/reports/{run_id}",
        )
    except WorkflowError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(exc.code) from exc


@app.command("wiki-draft")
def wiki_draft_cmd(research_slug: str = typer.Argument(...)) -> None:
    gbrain = _gbrain()
    typer.echo(wiki_draft(gbrain, research_slug))
