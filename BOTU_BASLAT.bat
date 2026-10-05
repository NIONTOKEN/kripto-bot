@echo off
title KriptoNexus Bot - CANLI MAINNET (BILGISAYAR)
color 0A
cls
echo.
echo  ============================================================
echo   KRIPTONEXUS AI TRADING BOT - CANLI MAINNET
echo  ============================================================
echo   Mod: GERCEK PARA (MAINNET)
echo   Dashboard: http://localhost:8000
echo   Durdurmak icin bu pencereyi kapatmaniz yeterlidir.
echo.
echo   Bot baslatiliyor, lutfen bekleyin...
echo  ============================================================
echo.

cd /d "c:\Users\DMC BİLGİSAYAR\OneDrive\Desktop\kripto ticareti"

:BASLAT
echo [%TIME%] Bot calisiyor...
".venv\Scripts\python.exe" -m app.main
echo.
echo [%TIME%] Bot kapandi! 10 saniye sonra otomatik yeniden baslatilacak...
echo (Tamamen kapatmak icin pencereyi [X] ile kapatin)
echo.
timeout /t 10 /nobreak >nul
goto BASLAT
