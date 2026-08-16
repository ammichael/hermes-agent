#!/usr/bin/env python3
"""Reclassifica conversas antigas do Companion que ficaram gravadas como api_server.

Conservador de propósito: só toca ids que PROVAM origem no Companion
(`n-companion-…`, `n-voice-…`). Uma sessão `api_<epoch>_<hash>` criada pelo
telefone é indistinguível de uma criada por qualquer outro cliente da API, e
reclassificá-la transformaria esse cliente numa fonte de push no telefone.

Uso:
    python scripts/companion_backfill_source.py            # dry-run
    python scripts/companion_backfill_source.py --apply
"""

import argparse
import sqlite3
from pathlib import Path

PROVABLE_PREFIXES = ("n-companion-", "n-voice-")


def backfill(db_path: str, *, dry_run: bool = True) -> int:
    """Devolve quantas linhas seriam (ou foram) reclassificadas."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM sessions WHERE source = 'api_server'"
        ).fetchall()
        targets = [
            row[0] for row in rows
            if any(str(row[0]).startswith(p) for p in PROVABLE_PREFIXES)
        ]
        if targets and not dry_run:
            conn.executemany(
                "UPDATE sessions SET source = 'companion_ios' WHERE id = ?",
                [(t,) for t in targets],
            )
            conn.commit()
        return len(targets)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="grava; sem isso é dry-run")
    parser.add_argument(
        "--db",
        default=str(Path("~/.hermes/state.db").expanduser()),
    )
    args = parser.parse_args()
    count = backfill(args.db, dry_run=not args.apply)
    verbo = "reclassificadas" if args.apply else "seriam reclassificadas"
    print(f"{count} conversas {verbo}")


if __name__ == "__main__":
    main()
