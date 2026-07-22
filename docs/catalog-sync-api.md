# Catalog sync API

This API is the catalog-only bridge between the local crawler database and the cloud service.
It never reads or writes users, sessions, OAuth state, wishlists, followed series, recommendations,
or notification preferences.

## Security

Set the same random token (at least 32 characters) as `ANISHELF_CATALOG_SYNC_TOKEN` on Render and
on the local machine. Until Render has this value, every sync endpoint returns HTTP 503. Requests
must send `Authorization: Bearer <token>`; the token must not be placed in a URL or committed to Git.

Generate a token in PowerShell:

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
[Convert]::ToHexString($bytes).ToLower()
```

## Version 1 endpoints

- `GET /api/catalog-sync/status`: catalog count, publisher counts, and current cursors.
- `GET /api/catalog-sync/manifest?after_id=0&limit=1000`: lightweight exact-diff pages.
- `GET /api/catalog-sync/books?after_id=0&limit=500`: full catalog snapshot pages.
- `GET /api/catalog-sync/changes?after=0&limit=500`: incremental changes after a cursor.
- `POST /api/catalog-sync/books`: validate and upsert at most 200 manga/novel records.

The full snapshot returns `snapshot_change_id`. A client records that value, completes the snapshot,
then consumes `/changes` after that cursor so edits made during the snapshot are not missed.

Manifest comparisons use `sync_hash` plus `sync_hash_version`, not the crawler's internal
`source_hash`. Version 1 hashes a fixed canonical set of catalog fields, so adding crawler fields or
reindexing legacy rows cannot turn the entire catalog into false differences. A future field-set
change must introduce a new sync hash version before clients switch to it.

Sparse peer records cannot erase an existing non-empty author, ISBN, cover, price, date, or explicit
rating. A cloud administrator's locked rating always wins. Uploads emit `sync_upload` change events;
clients must not echo those events back to the same peer.

## Difference check

After setting the local token and database variables, run:

```powershell
python -m app.catalog_sync_client --base-url https://anishelf-wmcu.onrender.com
```

The command reports matching rows, local-only rows, cloud-only rows, and rows whose source hashes
differ. It is read-only and includes at most 20 examples per difference category.
