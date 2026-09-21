"""Independence audit — syndicated copies must not multiply consensus.

"3+ independent sources agree" is the pipeline's consensus rule, but a wire
story republished by five outlets is ONE source wearing five outfits.
This module clusters derivative sources three ways:

  url     — identical canonical URL (scheme/www/trailing-slash/UTM stripped)
  body    — near-duplicate bodies (>=0.7 MinHash-verified Jaccard, reusing
            core/similarity.py)
  wire    — shared wire-service boilerplate (PRNewswire, Business Wire, ...)
            in the opening of the text, same story cluster by title overlap

Scores: cluster root (earliest fetched) keeps independence 1.0; members get
1/cluster_size. Unclustered notes get 1.0. Stored on notes.independence
(DB-cache, recomputable) and consumed by step 3's consensus counting and
the quality composite.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from hyperresearch.core.similarity import jaccard, shingle

WIRE_MARKERS = (
    "prnewswire", "pr newswire", "business wire", "businesswire",
    "globe newswire", "globenewswire", "(reuters)", "(ap)", "associated press",
    "accesswire", "newsfile corp",
)

_TRACKING_PARAMS_RE = re.compile(r"^(utm_|fbclid|gclid|ref$|source$)")
NEAR_DUP_THRESHOLD = 0.7


def canonical_url(url: str) -> str:
    """Normalize scheme/host/path/query so syndication mirrors collide."""
    p = urlparse(url.strip().lower())
    host = p.netloc.removeprefix("www.")
    path = p.path.rstrip("/")
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(p.query) if not _TRACKING_PARAMS_RE.match(k)
    ))
    return urlunparse(("https", host, path, "", query, ""))


def _wire_signature(body: str, title: str) -> str | None:
    """Wire-marker + body-opening signature for press-release clustering.

    Keyed on the body head, NOT the title — outlets retitle syndicated
    copy, but the wire text itself opens identically.
    """
    head = (body or "")[:1500].lower()
    marker = next((m for m in WIRE_MARKERS if m in head), None)
    if marker is None:
        return None
    tokens = re.findall(r"[a-z0-9]{4,}", head)[:10]
    return f"{marker}|{' '.join(tokens)}"


def compute_independence(
    vault: Any,
    tag: str | None = None,
    note_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Cluster derivative sources, write independence scores. Returns summary."""
    conn = vault.db
    query = (
        "SELECT n.id, n.source, n.title, n.created, nc.body_plain "
        "FROM notes n JOIN note_content nc ON nc.note_id = n.id "
        "WHERE n.source IS NOT NULL AND n.type NOT IN ('index')"
    )
    params: tuple[str, ...] = ()
    if note_ids is not None:
        wanted = [n for n in note_ids if n]
        if not wanted:
            return {"scored": 0, "clusters": [], "audited": []}
        placeholders = ",".join("?" for _ in wanted)
        query += f" AND n.id IN ({placeholders})"
        params = tuple(wanted)
    elif tag:
        query += " AND n.id IN (SELECT note_id FROM tags WHERE tag = ?)"
        params = (tag,)
    rows = [dict(r) for r in conn.execute(query, params).fetchall()]

    # Union-find over note ids
    parent: dict[str, str] = {r["id"]: r["id"] for r in rows}
    cluster_kind: dict[frozenset[str], str] = {}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str, kind: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
        cluster_kind[frozenset((a, b))] = kind

    # 1. Canonical-URL identity
    by_url: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_url.setdefault(canonical_url(r["source"]), []).append(r)
    for group in by_url.values():
        for other in group[1:]:
            union(group[0]["id"], other["id"], "url")

    # 2. Wire-service signature
    by_wire: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        sig = _wire_signature(r["body_plain"], r["title"])
        if sig:
            by_wire.setdefault(sig, []).append(r)
    for group in by_wire.values():
        for other in group[1:]:
            union(group[0]["id"], other["id"], "wire")

    # 3. Near-duplicate bodies (pairwise Jaccard on shingles; vaults are
    # small enough per-tag, and dedup's MinHash/LSH path exists for scale)
    shingles = {r["id"]: shingle(r["body_plain"] or "", n=vault.config.dedup.shingle_size) for r in rows}
    ids = [r["id"] for r in rows]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if find(ids[i]) == find(ids[j]):
                continue
            if jaccard(shingles[ids[i]], shingles[ids[j]]) >= NEAR_DUP_THRESHOLD:
                union(ids[i], ids[j], "body")

    # Materialize clusters; root = earliest fetched (the upstream original)
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)

    clusters: list[dict[str, Any]] = []
    scored = 0
    for members in groups.values():
        if len(members) == 1:
            conn.execute("UPDATE notes SET independence = 1.0 WHERE id = ?", (members[0]["id"],))
            scored += 1
            continue
        members.sort(key=lambda r: r["created"] or "")
        root = members[0]
        share = round(1.0 / len(members), 4)
        conn.execute("UPDATE notes SET independence = 1.0 WHERE id = ?", (root["id"],))
        kinds = {
            cluster_kind.get(frozenset((a["id"], b["id"])))
            for a in members for b in members
            if cluster_kind.get(frozenset((a["id"], b["id"])))
        }
        for m in members[1:]:
            conn.execute("UPDATE notes SET independence = ? WHERE id = ?", (share, m["id"]))
        scored += len(members)
        clusters.append({
            "root": root["id"],
            "members": [m["id"] for m in members[1:]],
            "size": len(members),
            "kind": "+".join(sorted(k for k in kinds if k)) or "mixed",
        })
    conn.commit()
    return {"scored": scored, "clusters": clusters, "audited": [r["id"] for r in rows]}


def cluster_evidence_independence(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Cluster on document_id and non-empty provenance. Empty provenance is unknown.

    Namespace is not an independence key. Distinct empty-provenance documents
    must not increment independent_source_count.
    """
    nodes: list[dict[str, Any]] = []
    for raw in items:
        nid = str(raw.get("id") or raw.get("note_id") or raw.get("document_id") or "")
        if not nid:
            continue
        document_id = str(raw.get("document_id") or nid)
        prov_raw = raw.get("provenance")
        provenance: list[str] = []
        if isinstance(prov_raw, list):
            provenance = [str(p).strip() for p in prov_raw if str(p).strip()]
        url = str(raw.get("url") or raw.get("source") or "").strip()
        if url and url not in provenance:
            provenance.append(url)
        nodes.append({"id": nid, "document_id": document_id, "provenance": provenance})
    if not nodes:
        return {"independent_source_count": 0, "clusters": [], "audited": []}

    parent = {n["id"]: n["id"] for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_doc: dict[str, list[str]] = {}
    by_prov: dict[str, list[str]] = {}
    for n in nodes:
        by_doc.setdefault(n["document_id"], []).append(n["id"])
        for p in n["provenance"]:
            by_prov.setdefault(p, []).append(n["id"])
    for group in by_doc.values():
        for other in group[1:]:
            union(group[0], other)
    for group in by_prov.values():
        for other in group[1:]:
            union(group[0], other)

    groups: dict[str, list[dict[str, Any]]] = {}
    for n in nodes:
        groups.setdefault(find(n["id"]), []).append(n)

    clusters: list[dict[str, Any]] = []
    independent = 0
    for members in groups.values():
        known = sorted({p for m in members for p in m["provenance"]})
        if known:
            independent += 1
        clusters.append({
            "members": [m["id"] for m in members],
            "document_ids": sorted({m["document_id"] for m in members}),
            "provenance": known,
            "independent": bool(known),
        })
    return {
        "independent_source_count": independent,
        "clusters": clusters,
        "audited": [n["id"] for n in nodes],
    }
