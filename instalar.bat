@echo off
echo ============================================
echo   INSTALANDO DEPENDENCIAS DEL BOT
echo ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python no esta instalado.
    echo Descargalo en https://www.python.org/downloads/
    echo Asegurate de marcar "Add Python to PATH" al instalar.
    pause
    exit /b
)

echo Python encontrado. Instalando librerias...
pip install -r requirements.txt

echo.
echo ============================================
echo   LISTO! Ahora edita config.env con tus
echo   API Keys de Binance y ejecuta run.bat
echo ============================================
pause
