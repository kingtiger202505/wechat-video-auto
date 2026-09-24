@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist keywords.txt (
  copy /y keywords.example.txt keywords.txt >nul
  echo 已创建 keywords.txt，请每行填一个关键词，保存后再双击 batch.bat
  notepad keywords.txt
  exit /b 0
)
call run.bat -f keywords.txt
