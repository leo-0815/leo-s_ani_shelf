"""Explicit, bounded reconciliation of existing Ching Win duplicates.

Never run at startup. Private conflicts are blocked; preimages stay in the DB,
and no private data is returned through the catalog sync protocol.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

from .db import transaction
from .book_identity import (_title, _edition, _volume, _isbn, compatible, author_tokens,
                            chingwin_source_kind, chingwin_product_id, remember_alias, identity_candidates)

REFERENCE_TABLES = ("wishlist_items", "collection_items", "recommendation_dismissals",
                    "release_history", "catalog_changes", "notification_deliveries",
                    "book_release_checks", "book_rating_checks")
PRIVATE_TABLES = {"wishlist_items", "collection_items", "recommendation_dismissals"}


class MergeBlocked(ValueError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def proof(old: dict, canonical: dict) -> str:
    return hashlib.sha256(_json([old, canonical]).encode()).hexdigest()


def pair_matches(old: dict, canonical: dict) -> bool:
    if not compatible(old, canonical) or old["publisher_id"] != canonical["publisher_id"]:
        return False
    if chingwin_source_kind(canonical) != "product" or canonical["source_key"] != chingwin_product_id(canonical):
        return False
    if chingwin_source_kind(old) == "product":
        return chingwin_product_id(old) == chingwin_product_id(canonical)
    if chingwin_source_kind(old) != "schedule":
        return False
    return bool((_isbn(old) and _isbn(old) == _isbn(canonical)) or
                (_title(old["title"]) == _title(canonical["title"])
                 and author_tokens(old.get("author")) & author_tokens(canonical.get("author"))))


def duplicate_plan(rows: list[dict]) -> dict:
    products, by_title, by_isbn = {}, defaultdict(list), defaultdict(list)
    for row in rows:
        if chingwin_source_kind(row) == "product" and row["source_key"] == chingwin_product_id(row):
            products[row["id"]] = row
            shape = (row["publisher_id"], row["media_type"], _edition(row), _volume(row))
            by_title[(*shape, _title(row["title"]))].append(row)
            if _isbn(row):
                by_isbn[(*shape, _isbn(row))].append(row)
    pairs, ambiguous = [], 0
    for old in rows:
        if old["id"] in products:
            continue
        shape = (old["publisher_id"], old["media_type"], _edition(old), _volume(old))
        related = {r["id"]: r for r in by_title[(*shape, _title(old["title"]))]}
        if _isbn(old):
            related.update({r["id"]: r for r in by_isbn[(*shape, _isbn(old))]})
        matches = [r for r in related.values() if pair_matches(old, r)]
        if len(matches) > 1:
            ambiguous += 1
        if len(matches) == 1:
            canonical = matches[0]
            pairs.append(dict(old_id=old["id"], canonical_id=canonical["id"], old_key=old["source_key"],
                              canonical_key=canonical["source_key"], title=canonical["title"],
                              proof=proof(old, canonical)))
    return {"pairs": sorted(pairs, key=lambda p: p["old_id"]), "ambiguous": ambiguous}


def _publisher_rows(cursor, *, lock=False):
    cursor.execute("SELECT b.*,p.code AS publisher_code FROM books b JOIN publishers p ON p.id=b.publisher_id "
                   "WHERE p.code='chingwin' ORDER BY b.id" + (" FOR UPDATE" if lock else ""))
    return cursor.fetchall()


def preview(limit=50, after_id=0) -> dict:
    with transaction() as connection, connection.cursor() as cursor:
        plan = duplicate_plan(_publisher_rows(cursor))
    return {"pairs": [p for p in plan["pairs"] if p["old_id"] > int(after_id)][:min(max(int(limit), 1), 50)], "total_pairs": len(plan["pairs"]),
            "ambiguous": plan["ambiguous"]}


def _reference_tables(cursor) -> list[str]:
    cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=DATABASE() "
                   "AND table_name IN (" + ",".join(["%s"] * len(REFERENCE_TABLES)) + ")", REFERENCE_TABLES)
    tables = {str(r.get("TABLE_NAME") or r.get("table_name")) for r in cursor.fetchall()}
    cursor.execute("SELECT table_name,column_name FROM information_schema.key_column_usage "
                   "WHERE referenced_table_schema=DATABASE() AND referenced_table_name='books'")
    allowed = tables | {"book_source_aliases", "catalog_book_merges"}
    if any(str(r.get("TABLE_NAME") or r.get("table_name")) not in allowed or
           str(r.get("COLUMN_NAME") or r.get("column_name")) !=
           ("canonical_book_id" if str(r.get("TABLE_NAME") or r.get("table_name")) == "catalog_book_merges" else "book_id")
           for r in cursor.fetchall()):
        raise MergeBlocked("unknown_reference")
    return [t for t in REFERENCE_TABLES if t in tables]


def _private_conflicts(refs: dict, old_id: int, new_id: int, cloud: bool):
    for table in PRIVATE_TABLES & refs.keys():
        old = [r for r in refs[table] if r["book_id"] == old_id]
        new = {r.get("user_id", 0): r for r in refs[table] if r["book_id"] == new_id}
        for row in old:
            other = new.get(row.get("user_id", 0))
            if other:
                if table == "collection_items" and row.get("id") != other.get("id"):
                    raise MergeBlocked("duplicate_collection")
                fields = set(row) - {"id", "book_id", "created_at", "updated_at"}
                if any(row.get(f) != other.get(f) for f in fields):
                    raise MergeBlocked("private_conflict")


def _move_references(cursor, refs: dict, old_id: int, new_id: int):
    for table, rows in refs.items():
        # Identical private entries collapse without overwriting any fields.
        if table in PRIVATE_TABLES:
            new_users = {r.get("user_id", 0) for r in rows if r["book_id"] == new_id}
            for row in rows:
                if row["book_id"] == old_id and row.get("user_id", 0) in new_users:
                    clause, args = "book_id=%s", [old_id]
                    if "user_id" in row:
                        clause += " AND user_id=%s"
                        args.append(row["user_id"])
                    cursor.execute(f"DELETE FROM {table} WHERE " + clause, args)
        elif table in {"book_release_checks", "book_rating_checks"}:
            old_rows = [r for r in rows if r["book_id"] == old_id]
            new_rows = [r for r in rows if r["book_id"] == new_id]
            if old_rows and new_rows:
                # Operational preimages are archived; retain the canonical SKU
                # check. Never silently clear a pending classification upload.
                pending = (table == "book_rating_checks" and old_rows[0].get("uploaded_at") is None
                           and old_rows[0].get("result") in {"confirmed", "revoked", "classified"})
                cursor.execute(f"DELETE FROM {table} WHERE book_id=%s", (new_id if pending else old_id,))
        cursor.execute(f"UPDATE {table} SET book_id=%s WHERE book_id=%s", (new_id, old_id))
    # Keep notification deduplication keys valid after the ID change.
    if "notification_deliveries" in refs:
        for row in refs["notification_deliveries"]:
            if row["book_id"] != old_id:
                continue
            parts = row["event_key"].split(":")
            if parts[0] not in {"release", "followed-series"} or len(parts) < 3 or parts[2] != str(old_id):
                continue
            parts[2] = str(new_id)
            key = ":".join(parts)
            cursor.execute("SELECT id FROM notification_deliveries WHERE channel=%s AND event_key=%s",
                           (row["channel"], key))
            if cursor.fetchone():
                cursor.execute("DELETE FROM notification_deliveries WHERE id=%s", (row["id"],))
            else:
                cursor.execute("UPDATE notification_deliveries SET event_key=%s WHERE id=%s", (key, row["id"]))


def merge_pair(old_id: int, canonical_id: int, expected_proof: str | None, *, cloud=True) -> dict:
    from .repository import BOOK_FIELDS, _crawler_write_data, _record_catalog_change
    from .models import book_content_hash
    with transaction() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM publishers WHERE code='chingwin' FOR UPDATE")
        publisher = cursor.fetchone()
        if not publisher:
            raise MergeBlocked("missing_publisher")
        # Current reads share PR46's publisher lock; stale previews are rejected.
        cursor.execute("SELECT b.*, 'chingwin' AS publisher_code FROM books b "
                       "WHERE b.publisher_id=%s AND b.id IN (%s,%s) ORDER BY b.id FOR UPDATE",
                       (publisher["id"], old_id, canonical_id))
        rows = {r["id"]: r for r in cursor.fetchall()}
        old, canonical = rows.get(old_id), rows.get(canonical_id)
        if not old or not canonical or old_id == canonical_id or not pair_matches(old, canonical):
            raise MergeBlocked("identity_changed")
        if expected_proof and proof(old, canonical) != expected_proof:
            raise MergeBlocked("stale_preview")
        # Refuse to choose among another equally plausible product variant.
        candidates = identity_candidates(cursor, publisher["id"], old)
        products = [r for r in candidates if r["id"] != old_id and pair_matches(old, r)]
        if len(candidates) > 200 or len(products) != 1 or products[0]["id"] != canonical_id:
            raise MergeBlocked("ambiguous_identity")
        refs = {}
        for table in _reference_tables(cursor):
            cursor.execute(f"SELECT * FROM {table} WHERE book_id IN (%s,%s) FOR UPDATE", (old_id, canonical_id))
            refs[table] = cursor.fetchall()
        _private_conflicts(refs, old_id, canonical_id, cloud)
        if old.get("rating_locked") and canonical.get("rating_locked") and any(
                old.get(f) != canonical.get(f) for f in ("content_rating", "rating_raw", "rating_source")):
            raise MergeBlocked("manual_rating_conflict")
        cursor.execute("SELECT * FROM book_source_aliases WHERE book_id IN (%s,%s) FOR UPDATE", (old_id, canonical_id))
        aliases = cursor.fetchall()
        # Durable, DB-only recovery snapshot. Never returned by preview/sync.
        cursor.execute("INSERT INTO catalog_book_merges (old_book_id,canonical_book_id,old_book_json,"
                       "canonical_before_json,references_json,aliases_json) VALUES (%s,%s,%s,%s,%s,%s)",
                       (old_id, canonical_id, _json(old), _json(canonical), _json(refs), _json(aliases)))
        data = {f: old.get(f) for f in BOOK_FIELDS}
        data.update(publisher_code="chingwin", source_key=canonical["source_key"])
        merged = _crawler_write_data(canonical, data)
        if old.get("rating_locked"):
            for f in ("content_rating", "rating_raw", "rating_source", "rating_confidence", "rating_checked_at", "rating_parser_version"):
                merged[f] = old.get(f)
        merged["source_hash"] = book_content_hash(merged)
        cursor.execute("UPDATE books SET " + ",".join(f"{f}=%s" for f in BOOK_FIELDS) +
                       ",rating_locked=%s,first_seen_at=%s WHERE id=%s",
                       [*[merged[f] for f in BOOK_FIELDS], bool(old.get("rating_locked") or canonical.get("rating_locked")),
                        min(str(v) for v in (old.get("first_seen_at"), canonical.get("first_seen_at")) if v), canonical_id])
        _move_references(cursor, refs, old_id, canonical_id)
        cursor.execute("UPDATE book_source_aliases SET book_id=%s WHERE book_id=%s", (canonical_id, old_id))
        remember_alias(cursor, publisher["id"], canonical_id, old, "merged")
        remember_alias(cursor, publisher["id"], canonical_id, canonical, "canonical")
        cursor.execute("UPDATE catalog_book_merges SET canonical_book_id=%s WHERE canonical_book_id=%s", (canonical_id, old_id))
        cursor.execute("DELETE FROM books WHERE id=%s", (old_id,))
        _record_catalog_change(cursor, canonical_id, "merged", merged["source_hash"],
                               "manual" if cloud else "cloud_pull", ("source_aliases",))
        cursor.execute("UPDATE catalog_book_merges SET canonical_after_hash=%s WHERE old_book_id=%s", (merged["source_hash"], old_id))
    return {"old_id": old_id, "canonical_id": canonical_id, "merged": True}


def apply_pairs(pairs: Any) -> dict:
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= 50:
        raise ValueError("整併每批需 1 至 50 組")
    if any(not isinstance(p, dict) or type(p.get("old_id")) is not int or type(p.get("canonical_id")) is not int
           or not isinstance(p.get("proof"), str) or len(p["proof"]) != 64 for p in pairs):
        raise ValueError("整併配對格式錯誤")
    merged, blocked = [], []
    for pair in pairs:
        try:
            merged.append(merge_pair(pair["old_id"], pair["canonical_id"], pair["proof"]))
        except MergeBlocked as exc:
            blocked.append({"old_id": pair["old_id"], "reason": str(exc)})
    return {"merged": len(merged), "blocked": blocked}


def resolve_book_id(cursor, book_id: int) -> int:
    cursor.execute("SELECT canonical_book_id FROM catalog_book_merges WHERE old_book_id=%s", (book_id,))
    row = cursor.fetchone()
    return int(row["canonical_book_id"]) if row and "canonical_book_id" in row else book_id


def previous_ids(items: list[dict]) -> list[dict]:
    ids = sorted({int(b["id"]) for b in items if b})
    if not ids:
        return items
    with transaction() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT old_book_id,canonical_book_id FROM catalog_book_merges WHERE canonical_book_id IN (" +
                       ",".join(["%s"] * len(ids)) + ")", ids)
        aliases = defaultdict(list)
        for row in cursor.fetchall():
            aliases[int(row["canonical_book_id"])].append(int(row["old_book_id"]))
    return [{**b, **({"previous_ids": aliases[int(b["id"])]} if aliases[int(b["id"])] else {})} for b in items]


def alias_page(after_id=0, limit=100) -> dict:
    from .repository import BOOK_FIELDS, serialize_row
    limit = min(max(int(limit), 1), 200)
    with transaction() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT DISTINCT b.*,p.code AS publisher_code FROM books b JOIN publishers p ON p.id=b.publisher_id "
                       "JOIN book_source_aliases a ON a.book_id=b.id AND a.alias_key<>b.source_key WHERE b.id>%s ORDER BY b.id LIMIT %s", (int(after_id), limit+1))
        rows = cursor.fetchall()
        items = []
        selected = rows[:limit]
        aliases_by_id = defaultdict(list)
        if selected:
            cursor.execute("SELECT book_id,alias_key FROM book_source_aliases WHERE book_id IN (" +
                           ",".join(["%s"] * len(selected)) + ") ORDER BY book_id,alias_key",
                           [r["id"] for r in selected])
            for alias in cursor.fetchall():
                aliases_by_id[alias["book_id"]].append(alias["alias_key"])
        for row in rows[:limit]:
            aliases = [key for key in aliases_by_id[row["id"]] if key != row["source_key"]]
            # Strict public allowlist; never include locked grades/private audit.
            public = {k: row.get(k) for k in ("publisher_code", "source_key", *BOOK_FIELDS)}
            items.append({"book": serialize_row(public), "aliases": aliases})
    return {"items": items, "next_after_id": rows[min(len(rows), limit)-1]["id"] if rows else int(after_id),
            "has_more": len(rows)>limit}
