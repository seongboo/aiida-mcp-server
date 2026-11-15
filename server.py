"""Read-only MCP server exposing an AiiDA profile to Claude.

Tools:
  - list_processes(state, process_label, limit)
  - get_node(pk)
  - query_structures(formula, elements, limit)

Design rules:
  * Read-only: no store/submit/delete calls anywhere in this file.
  * Output is a short summary; large arrays are never returned.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from functools import reduce
from typing import Any

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("aiida-mcp-server")

MAX_LIMIT = 100
MAX_TEXT = 2000
SCAN_LIMIT = 5000  # max StructureData nodes scanned per query_structures call

_profile_loaded = False


def _ensure_profile() -> None:
    """Load the AiiDA profile once (env AIIDA_PROFILE, else the default profile)."""
    global _profile_loaded
    if not _profile_loaded:
        from aiida import load_profile

        load_profile(os.environ.get("AIIDA_PROFILE") or None)
        _profile_loaded = True


def _clip(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _truncate(obj: Any) -> Any:
    """JSON-dump and cut to MAX_TEXT characters so context cannot blow up."""
    text = json.dumps(obj, default=str, ensure_ascii=False)
    if len(text) <= MAX_TEXT:
        return obj
    return {"truncated": True, "preview": text[:MAX_TEXT]}


def _iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


@mcp.tool()
def list_processes(
    state: str | None = None,
    process_label: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List recent AiiDA processes (CalcJob / WorkChain / calcfunction ...), newest first.

    Args:
        state: Optional process_state filter: created, waiting, running,
            finished, excepted, killed.
        process_label: Optional exact process label, e.g. "PwBaseWorkChain".
        limit: Max rows to return (capped at 100).
    """
    _ensure_profile()
    from aiida.orm import ProcessNode, QueryBuilder

    filters: dict[str, Any] = {}
    if state:
        filters["attributes.process_state"] = state.lower()
    if process_label:
        filters["attributes.process_label"] = process_label

    qb = QueryBuilder()
    qb.append(
        ProcessNode,
        tag="p",
        filters=filters,
        project=[
            "id",
            "label",
            "ctime",
            "attributes.process_label",
            "attributes.process_state",
            "attributes.exit_status",
        ],
    )
    qb.order_by({"p": {"ctime": "desc"}})
    qb.limit(_clip(limit))

    return [
        {
            "pk": pk,
            "label": label,
            "ctime": _iso(ctime),
            "process_label": plabel,
            "process_state": pstate,
            "exit_status": exit_status,
        }
        for pk, label, ctime, plabel, pstate, exit_status in qb.all()
    ]


@mcp.tool()
def get_node(pk: int) -> dict:
    """Return a compact summary of one node (by PK). Large arrays are not returned.

    Includes type-specific fields for process nodes, Dict, and StructureData,
    plus incoming/outgoing link counts.
    """
    _ensure_profile()
    from aiida import orm

    try:
        node = orm.load_node(int(pk))
    except Exception as exc:  # NotExistent, MultipleObjectsError, ...
        return {"error": f"{type(exc).__name__}: {exc}"}

    info: dict[str, Any] = {
        "pk": node.pk,
        "uuid": node.uuid,
        "node_type": node.node_type,
        "label": node.label,
        "description": node.description,
        "ctime": _iso(node.ctime),
        "n_incoming": len(node.base.links.get_incoming().all()),
        "n_outgoing": len(node.base.links.get_outgoing().all()),
    }

    if isinstance(node, orm.ProcessNode):
        info.update(
            process_label=node.process_label,
            process_state=node.process_state.value if node.process_state else None,
            exit_status=node.exit_status,
            exit_message=node.exit_message,
        )
    elif isinstance(node, orm.Dict):
        info["dict"] = _truncate(node.get_dict())
    elif isinstance(node, orm.StructureData):
        info.update(
            formula=node.get_formula(),
            n_sites=len(node.sites),
            pbc=list(node.pbc),
            cell=[list(map(float, v)) for v in node.cell],
        )
    elif isinstance(node, orm.Float | orm.Int | orm.Str | orm.Bool):
        info["value"] = node.value
    else:
        info["attribute_keys"] = sorted(node.base.attributes.keys())[:50]

    return info


def _parse_formula(formula: str) -> Counter:
    counts: Counter = Counter()
    for sym, num in re.findall(r"([A-Z][a-z]?)(\d*)", formula):
        counts[sym] += int(num) if num else 1
    return counts


def _site_counts(node: Any) -> Counter:
    """Element counts from sites/kinds (no ase needed). Alloy sites count each symbol once."""
    counts: Counter = Counter()
    kinds = {k.name: k for k in node.kinds}
    for site in node.sites:
        for sym in kinds[site.kind_name].symbols:
            counts[sym] += 1
    return counts


def _reduce(counts: Counter) -> dict[str, int]:
    g = reduce(math.gcd, counts.values()) if counts else 1
    return {k: v // g for k, v in counts.items()}


@mcp.tool()
def query_structures(
    formula: str | None = None,
    elements: list[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search StructureData nodes by reduced formula and/or contained elements.

    Args:
        formula: Chemical formula, e.g. "BaTiO3". Matched on the *reduced*
            composition, so a 2x2x2 supercell of BaTiO3 also matches.
        elements: Optional list of element symbols that must all be present.
        limit: Max rows to return (capped at 100).

    Note: formula matching runs in Python over at most 5000 newest structures
    (the formula is not an indexed DB field).
    """
    _ensure_profile()
    from aiida.orm import QueryBuilder, StructureData

    target = _reduce(_parse_formula(formula)) if formula else None
    required = set(elements or [])
    cap = _clip(limit)

    qb = QueryBuilder()
    qb.append(StructureData, tag="s", project=["*"])
    qb.order_by({"s": {"ctime": "desc"}})
    qb.limit(SCAN_LIMIT)

    out: list[dict] = []
    for (node,) in qb.iterall():
        counts = _site_counts(node)
        if target is not None and _reduce(counts) != target:
            continue
        if required and not required.issubset(counts):
            continue
        out.append(
            {
                "pk": node.pk,
                "label": node.label,
                "formula": node.get_formula(),
                "n_sites": len(node.sites),
                "ctime": _iso(node.ctime),
            }
        )
        if len(out) >= cap:
            break
    return out


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
