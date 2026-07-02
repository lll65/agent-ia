@echo off
REM ============================================================
REM  Lanceur MasterAgent-Gros — double-clique ce fichier.
REM  Il se place dans le bon dossier, active le venv et lance l'agent.
REM ============================================================
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo [ERREUR] Le dossier venv est introuvable.
    echo Ouvre une fenetre noire ici et fais d'abord :
    echo     python -m venv venv
    echo     venv\Scripts\activate
    echo     pip install -r requirements.txt
    pause
    exit /b 1
)

call venv\Scripts\activate.bat
echo.
echo === MasterAgent-Gros demarre... ouvre http://localhost:7860 dans ton navigateur ===
echo === Pour arreter : ferme cette fenetre ou appuie sur Ctrl+C ===
echo.
python main.py

echo.
echo === Agent arrete. Appuie sur une touche pour fermer. ===
pause
