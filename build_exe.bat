@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0"

echo.
echo ==========================================
echo   打包 ShareFiles 为单文件 exe
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 python，请先安装 Python 并把它加进 PATH。
    goto fail
)

python -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
    echo [1/3] 未装 PyInstaller，正在安装 ...
    python -m pip install --upgrade pyinstaller
    if errorlevel 1 goto fail
) else (
    echo [1/3] PyInstaller 已就绪
)

echo [2/3] 清理上次的产物 ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

if exist ShareFiles.spec (
    echo [3/3] 按 ShareFiles.spec 打包，请耐心等 ...
    python -m PyInstaller --noconfirm ShareFiles.spec
) else (
    echo [3/3] 没找到 ShareFiles.spec，改用命令行参数打包 ...
    python -m PyInstaller --noconfirm --onefile --windowed --name ShareFiles ^
      --icon "icon.ico" --add-data "icon.ico;." ^
      --collect-all qfluentwidgets --collect-all qframelesswindow ^
      app.py
)
if errorlevel 1 goto fail

echo.
echo ==========================================
echo   打包完成
echo ==========================================
echo   产物: %CD%\dist\ShareFiles.exe
for %%F in (dist\ShareFiles.exe) do echo   大小: %%~zF 字节
echo.
echo   首次运行 Windows 可能弹防火墙询问，请选「允许访问」。
echo   打包版和脚本版共用同一份配置：
echo   %USERPROFILE%\.sharefiles\config.json
echo.
start "" explorer "%CD%\dist"
pause
exit /b 0

:fail
echo.
echo [失败] 打包没能完成，上面有具体报错。
echo.
pause
exit /b 1
