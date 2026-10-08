@echo off
chcp 65001 > nul
set /p commit_msg="Enter a description of the commit: "

echo.
echo Добавление файлов...
git add .

echo.
echo Создание коммита...
git commit -m "%commit_msg%"

echo.
echo Отправка на GitHub...
git push

echo.
echo Проект успешно обновлен!
pause