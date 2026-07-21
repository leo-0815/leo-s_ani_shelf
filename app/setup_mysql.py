from __future__ import annotations

import getpass
import secrets
from pathlib import Path

from .config import ROOT


def main() -> None:
    try:
        import pymysql
    except ImportError as exc:
        raise SystemExit("請先執行：python -m pip install -r requirements.txt") from exc

    print("AniShelf MySQL 初始化")
    print("root 密碼只用於這一次建立專用資料庫帳號，不會被保存。")
    host = input("MySQL 主機 [127.0.0.1]: ").strip() or "127.0.0.1"
    port_text = input("MySQL 連接埠 [3306]: ").strip() or "3306"
    root_user = input("MySQL 管理帳號 [root]（通常直接按 Enter）: ").strip() or "root"
    root_password = getpass.getpass("管理帳號密碼: ")
    app_password = secrets.token_urlsafe(24)

    try:
        connection = pymysql.connect(
            host=host,
            port=int(port_text),
            user=root_user,
            password=root_password,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=5,
            ssl={"check_hostname": False},
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE DATABASE IF NOT EXISTS anishelf "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            cursor.execute(
                "CREATE USER IF NOT EXISTS 'anishelf_app'@'localhost' IDENTIFIED BY %s",
                (app_password,),
            )
            cursor.execute(
                "ALTER USER 'anishelf_app'@'localhost' IDENTIFIED BY %s",
                (app_password,),
            )
            cursor.execute("GRANT ALL PRIVILEGES ON anishelf.* TO 'anishelf_app'@'localhost'")
        connection.close()
    except Exception as exc:
        raise SystemExit(f"MySQL 初始化失敗：{exc}") from exc

    env_path = ROOT / ".env"
    env_path.write_text(
        "\n".join(
            [
                f"ANISHELF_DB_HOST={host}",
                f"ANISHELF_DB_PORT={port_text}",
                "ANISHELF_DB_NAME=anishelf",
                "ANISHELF_DB_USER=anishelf_app",
                f"ANISHELF_DB_PASSWORD={app_password}",
                "ANISHELF_DB_SSL_MODE=preferred",
                "ANISHELF_HOST=127.0.0.1",
                "ANISHELF_PORT=8765",
                "ANISHELF_CLOUD_MODE=false",
                "ANISHELF_AUTO_UPDATE=true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    from .db import ensure_schema, ping

    ensure_schema()
    info = ping()
    print(f"完成：資料庫 {info['database_name']}，MySQL {info['version']}")
    print(f"設定已保存於 {env_path}（已被 Git 忽略）。")


if __name__ == "__main__":
    main()
