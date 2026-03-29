@echo off
setlocal enabledelayedexpansion
cd /d "C:\github vs\balatro"
set LOVE=G:\SteamLibrary\steamapps\common\Balatro\Balatro.exe
set LOVELY=G:\SteamLibrary\steamapps\common\Balatro\version.dll
set UVX=C:\Users\Tyler\AppData\Roaming\Python\Python311\Scripts\uvx.exe
set PYTHON=C:\github vs\balatro\.venv\Scripts\python.exe

echo Starting 6 headless Balatro instances...

start "Balatro 12346" "%UVX%" balatrobot serve --port 12346 --headless --fast --love-path "%LOVE%" --lovely-path "%LOVELY%"
timeout /t 3 >nul
start "Balatro 12347" "%UVX%" balatrobot serve --port 12347 --headless --fast --love-path "%LOVE%" --lovely-path "%LOVELY%"
timeout /t 3 >nul
start "Balatro 12348" "%UVX%" balatrobot serve --port 12348 --headless --fast --love-path "%LOVE%" --lovely-path "%LOVELY%"
timeout /t 3 >nul
start "Balatro 12349" "%UVX%" balatrobot serve --port 12349 --headless --fast --love-path "%LOVE%" --lovely-path "%LOVELY%"
timeout /t 3 >nul
start "Balatro 12350" "%UVX%" balatrobot serve --port 12350 --headless --fast --love-path "%LOVE%" --lovely-path "%LOVELY%"
timeout /t 3 >nul
start "Balatro 12351" "%UVX%" balatrobot serve --port 12351 --headless --fast --love-path "%LOVE%" --lovely-path "%LOVELY%"


echo Waiting for all instances to load...
timeout /t 20 >nul

echo Creating log directories...
for %%P in (12346 12347 12348 12349 12350 12351) do (
    mkdir "bot_log\%%P" 2>nul
)
mkdir "bot_log\wins" 2>nul

echo Computing session log number...
"%PYTHON%" -c "import re; from pathlib import Path; p=Path('bot_log'); nums=[int(m.group(1)) for f in p.glob('*/game_*.log') if (m:=re.match('game_([0-9]+)[.]log',f.name))]; open(p/'next_num.txt','w').write(str((max(nums)+1) if nums else 1))"

echo Starting bots...
start "Bot 12346" "%PYTHON%" bot.py --start --port 12346 --games 1000 --uvx "%UVX%" --love-path "%LOVE%" --lovely-path "%LOVELY%"
timeout /t 2 >nul
start "Bot 12347" "%PYTHON%" bot.py --start --port 12347 --games 1000 --uvx "%UVX%" --love-path "%LOVE%" --lovely-path "%LOVELY%"
timeout /t 2 >nul
start "Bot 12348" "%PYTHON%" bot.py --start --port 12348 --games 1000 --uvx "%UVX%" --love-path "%LOVE%" --lovely-path "%LOVELY%"
timeout /t 2 >nul
start "Bot 12349" "%PYTHON%" bot.py --start --port 12349 --games 1000 --uvx "%UVX%" --love-path "%LOVE%" --lovely-path "%LOVELY%"
timeout /t 2 >nul
start "Bot 12350" "%PYTHON%" bot.py --start --port 12350 --games 1000 --uvx "%UVX%" --love-path "%LOVE%" --lovely-path "%LOVELY%"
timeout /t 2 >nul
start "Bot 12351" "%PYTHON%" bot.py --start --port 12351 --games 1000 --uvx "%UVX%" --love-path "%LOVE%" --lovely-path "%LOVELY%"


:monitor
cls
echo  Balatro Bot Progress
echo  ====================
echo.
for %%P in (12346 12347 12348 12349 12350 12351) do (
    set "prog=starting..."
    if exist "bot_log\%%P\progress.txt" (
        for /f "usebackq delims=" %%L in ("bot_log\%%P\progress.txt") do set "prog=%%L"
    )
    echo   Port %%P:  !prog!
)
echo.
echo  Logs: bot_log\[port]\game_NNN.log   ^| Ctrl+C to exit
timeout /t 5 /nobreak >nul
goto monitor
