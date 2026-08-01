@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
"C:\Users\HARRYHOA\AppData\Local\Programs\Python\Python312\python.exe" run.py %*
