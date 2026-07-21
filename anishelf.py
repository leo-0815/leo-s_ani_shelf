from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / ".vendor"
if VENDOR.exists():
    sys.path.insert(0, str(VENDOR))


def main() -> None:
    command = sys.argv[1].lower() if len(sys.argv) > 1 else "run"
    if command == "install":
        VENDOR.mkdir(exist_ok=True)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--target",
                str(VENDOR),
                "-r",
                str(ROOT / "requirements.txt"),
            ],
            check=True,
        )
    elif command == "setup":
        from app.setup_mysql import main as setup_main

        setup_main()
    elif command == "run":
        from app.server import main as server_main

        server_main()
    elif command == "backfill":
        from app.crawler import run_backfill
        from app.db import ensure_schema

        ensure_schema()
        source_code = sys.argv[2].lower() if len(sys.argv) > 2 else "all"
        job_id = run_backfill(source_code)
        print(f"大型回填完成（工作 #{job_id}）")
    elif command == "update":
        from app.crawler import run_incremental
        from app.db import ensure_schema

        ensure_schema()
        source_code = sys.argv[2].lower() if len(sys.argv) > 2 else "all"
        job_id = run_incremental(source_code)
        print(f"增量更新完成（工作 #{job_id}）")
    elif command == "status":
        from app.db import ensure_schema, transaction
        from app.repository import stats

        ensure_schema()
        with transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT p.code, COUNT(b.id) AS count, MIN(b.release_date) AS first_date, "
                    "MAX(b.release_date) AS last_date FROM publishers p LEFT JOIN books b "
                    "ON b.publisher_id = p.id GROUP BY p.code ORDER BY p.code"
                )
                publishers = cursor.fetchall()
                cursor.execute(
                    "SELECT source_code, cursor_date, last_mode, backfill_completed, last_success_at "
                    "FROM source_sync_state ORDER BY source_code"
                )
                sync = cursor.fetchall()
                cursor.execute(
                    "SELECT ROUND(SUM(data_length + index_length) / 1024, 1) AS database_kb "
                    "FROM information_schema.tables WHERE table_schema = DATABASE()"
                )
                size = cursor.fetchone()
                cursor.execute("SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1")
                latest_job = cursor.fetchone()
        print(json.dumps({"stats": stats(0), "publishers": publishers, "sync": sync, "latest_job": latest_job, **size}, default=str, ensure_ascii=False, indent=2))
    elif command == "migrate":
        from app.db import ensure_schema

        ensure_schema()
        print("資料庫結構已更新")
    elif command == "reindex":
        from app.db import ensure_schema
        from app.repository import rebuild_book_metadata

        ensure_schema()
        count = rebuild_book_metadata()
        print(f"已重新整理 {count} 筆書目的系列、卷數與版本欄位")
    elif command == "restore":
        if len(sys.argv) < 3:
            raise SystemExit("用法：python anishelf.py restore <anishelf-export.json>")
        from app.backup import restore_export
        from app.db import ensure_schema

        ensure_schema()
        result = restore_export(Path(sys.argv[2]).expanduser().resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif command == "export":
        from app.db import ensure_schema
        from app.repository import export_catalog

        ensure_schema()
        target = (
            Path(sys.argv[2]).expanduser().resolve()
            if len(sys.argv) > 2
            else ROOT / "backups" / "anishelf-export.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                export_catalog(0),
                default=str,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"已匯出備份：{target}")
    else:
        raise SystemExit(
            "用法：python anishelf.py "
            "[install|setup|run|backfill|update|status|migrate|reindex|export|restore]"
        )


if __name__ == "__main__":
    main()
