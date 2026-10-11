@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo Vintage Story Modpack Builder - Git update
echo ================================================
echo.

where git >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git was not found in PATH.
    pause
    exit /b 1
)

if not exist ".git" (
    echo [ERROR] This folder is not a Git repository.
    pause
    exit /b 1
)

set "commit_msg="
set /p "commit_msg=Enter a description of the commit: "
if not defined commit_msg set "commit_msg=Update project"

echo.
echo [1/4] Git status...
git status --short

echo.

echo [2/4] Adding files...
git add .
if errorlevel 1 (
    echo [ERROR] git add failed.
    pause
    exit /b 1
)

echo.
echo [3/4] Creating commit...
git diff --cached --quiet
if not errorlevel 1 (
    echo [INFO] No staged changes. Nothing to commit.
    pause
    exit /b 0
)

git commit -m "%commit_msg%"
if errorlevel 1 (
    echo [ERROR] git commit failed.
    pause
    exit /b 1
)

echo.
echo [4/4] Sending to GitHub...
git push
if errorlevel 1 (
    echo [ERROR] git push failed. The commit exists locally and can be pushed later.
    pause
    exit /b 1
)

echo.
echo [OK] Project updated and pushed to GitHub.
pause
