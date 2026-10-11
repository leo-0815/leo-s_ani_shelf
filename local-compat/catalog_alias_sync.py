"""Cloud public identity aliases; no account data crosses the sync protocol."""
from .db import transaction
from .repository import upsert_book
from .book_identity import remember_alias
from .catalog_reconciliation import merge_pair


def apply_alias_item(item, record_from_peer):
    book = item["book"]
    if book.get("publisher_code") != "chingwin":
        raise ValueError("Only Ching Win source aliases are supported")
    upsert_book(record_from_peer(book), change_origin="cloud_pull",
                source_hash_override=book["source_hash"])
    with transaction() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT b.*,p.code AS publisher_code FROM books b JOIN publishers p ON p.id=b.publisher_id "
                       "WHERE p.code='chingwin' AND b.source_key=%s", (book["source_key"],))
        canonical = cursor.fetchone()
    if not canonical:
        raise RuntimeError("Canonical source key was not promoted; pending changes preserved")
    for key in item["aliases"]:
        if not isinstance(key, str) or not 1 <= len(key) <= 190:
            raise ValueError("Invalid public source alias")
        with transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM books WHERE publisher_id=%s AND source_key=%s",
                           (canonical["publisher_id"], key))
            existing = cursor.fetchone()
        if existing and int(existing["id"]) != int(canonical["id"]):
            merge_pair(int(existing["id"]), int(canonical["id"]), None, cloud=False)
        with transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM publishers WHERE id=%s FOR UPDATE", (canonical["publisher_id"],))
            cursor.fetchone()
            remember_alias(cursor, canonical["publisher_id"], canonical["id"],
                           {**canonical, "source_key": key}, "cloud_alias")


def pull_aliases(client, record_from_peer):
    if client.status().get("source_aliases_version") != 1:
        return 0
    after, count = 0, 0
    while True:
        page = client._request("GET", f"/api/catalog-sync/aliases?after_id={after}&limit=100")
        for item in page.get("items", []):
            apply_alias_item(item, record_from_peer)
            count += 1
        following = int(page.get("next_after_id", after))
        if not page.get("has_more"):
            return count
        if following <= after:
            raise RuntimeError("Source aliases did not advance; uploads stopped")
        after = following
