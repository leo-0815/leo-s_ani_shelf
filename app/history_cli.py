from __future__ import annotations

from .db import ensure_schema
from .history_runner import run_history_session


OPTIONS = {
    "1": "30m",
    "2": "60m",
    "3": "year",
    "4": "continuous",
}


def main() -> None:
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
    ensure_schema()
    run_history_session(mode)


if __name__ == "__main__":
    main()
