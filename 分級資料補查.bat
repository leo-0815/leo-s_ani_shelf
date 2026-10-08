@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 分級補查：每家最多10本輪巡；先保存本機，再同步至雲端。
echo 出版社代碼：chingwin=青文 spp=尖端 tongli=東立
echo 輸入 all 或多家代碼，以英文逗號分隔。
set "sources=all"
set /p sources=出版社 [all]:
echo 1. 先比對雲端  2. 先下載雲端較新書目再比對
set "preflight=compare"
set /p prepare=選擇 [1]:
if "%prepare%"=="2" set "preflight=pull"
echo 1. 30分鐘
echo 2. 1小時
echo 3. 完成目前可補查範圍
echo 4. 持續執行直到Ctrl+C
echo 5. 唯讀測試10筆（不存檔、不上傳）
set /p choice=選擇 [1]: 
set "mode=30m"
if "%choice%"=="2" set "mode=60m"
if "%choice%"=="3" set "mode=complete"
if "%choice%"=="4" set "mode=continuous"
if "%choice%"=="5" (
 python anishelf.py ratings --dry-run --limit 10 --sources "%sources%"
) else (
 python anishelf.py ratings --mode %mode% --sources "%sources%" --preflight %preflight% --sync
)
echo 已保存成功提交的進度；若同步失敗，下次會重試尚未上傳的分級。
pause
