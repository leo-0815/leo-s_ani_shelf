from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

OPTIONS = {
    "1": "30m",
    "2": "60m",
    "3": "year",
    "4": "continuous",
}


def main(argv: Sequence[str] = ()) -> None:
    parser = argparse.ArgumentParser(description="AniShelf 歷史書目更新")
    parser.add_argument("--preflight", action="store_true", help="只檢查工具與既有報告，不連線")
    parser.add_argument("--project-dir", type=Path, help="指定要檢查的既有專案（僅限預檢）")
    args = parser.parse_args(argv)
    if args.project_dir is not None and not args.preflight:
        parser.error("--project-dir 須搭配 --preflight")
    if args.preflight:
        from .history_preflight import show_history_preflight
        try:
            show_history_preflight(args.project_dir)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        return

    print("\nAniShelf 歷史書目更新")
    print("[1] 自動補查系列缺卷")
    print("[2] 地毯式回填（從斷點續跑）")
    print("[3] 唯讀預檢（不連線）")
    print("[Q] 離開")
    choice = input("請輸入選項：").strip().upper()
    if choice == "1":
        from .series_backfill_runner import run_interactive
        run_interactive()
    elif choice == "2":
        run_full_menu()
    elif choice == "3":
        main(("--preflight",))
    elif choice != "Q":
        raise SystemExit("無效選項。")


def run_full_menu() -> None:
    print("\n========================================")
    print("       AniShelf 歷史資料更新工具")
    print("========================================\n")
    print("執行前會先把雲端較新的書目補到本機。")
    print("每批完成後保存 checkpoint；下次會帶緩衝地繼續。")
    print("結束後會把已完成的本機書目同步回雲端。\n")
    print("[1] 執行 30 分鐘")
    print("[2] 執行 1 小時")
    print("[3] 執行到目前年度區間完成")
    print("[4] 持續執行，直到按 Ctrl+C 手動停止")
    print("[Q] 離開\n")
    choice = input("請輸入選項：").strip().upper()
    if choice == "Q":
        return
    mode = OPTIONS.get(choice)
    if not mode:
        raise SystemExit("無效選項。")
    print("\n即將開始。請保持此視窗開啟。")
    print("持續模式可按 Ctrl+C；最後完成批次的 checkpoint 會保留。\n")
    from .db import ensure_schema
    from .history_runner import run_history_session

    ensure_schema()
    run_history_session(mode)


if __name__ == "__main__":
    main(sys.argv[1:])
