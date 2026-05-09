@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "REPO_URL=https://github.com/bigboy4567/LedFx_to_ICUE_bridge.git"
set "DEFAULT_BRANCH=main"

cd /d "%~dp0"

rem Avoid inherited Git environment variables pointing outside this folder.
set "GIT_DIR="
set "GIT_WORK_TREE="
set "GIT_INDEX_FILE="

where git >nul 2>nul
if errorlevel 1 (
    echo Git est introuvable. Installe Git pour Windows puis relance ce fichier.
    pause
    exit /b 1
)

if not exist ".git" (
    echo Initialisation du depot Git local...
    git init
    if errorlevel 1 goto fail
    git branch -M "%DEFAULT_BRANCH%"
    if errorlevel 1 goto fail
)

call :ensure_ignore "*.log"
call :ensure_ignore "__pycache__/"
call :ensure_ignore "*.py[cod]"
call :ensure_ignore ".venv/"
call :ensure_ignore "venv/"
call :ensure_ignore "env/"

git remote get-url origin >nul 2>nul
if errorlevel 1 (
    echo Ajout du depot distant origin...
    git remote add origin "%REPO_URL%"
    if errorlevel 1 goto fail
) else (
    for /f "usebackq delims=" %%R in (`git remote get-url origin`) do set "ORIGIN_URL=%%R"
    if /I not "!ORIGIN_URL!"=="%REPO_URL%" (
        echo.
        echo Le remote origin actuel est:
        echo !ORIGIN_URL!
        echo.
        set /p "CHANGE_REMOTE=Le remplacer par %REPO_URL% ? [o/N] "
        if /I "!CHANGE_REMOTE!"=="o" (
            git remote set-url origin "%REPO_URL%"
            if errorlevel 1 goto fail
        ) else (
            echo Operation annulee.
            pause
            exit /b 1
        )
    )
)

for /f "delims=" %%B in ('git branch --show-current 2^>nul') do set "BRANCH=%%B"
if not defined BRANCH (
    set "BRANCH=%DEFAULT_BRANCH%"
    git checkout -B "%DEFAULT_BRANCH%" >nul 2>nul
    if errorlevel 1 (
        git branch -M "%DEFAULT_BRANCH%"
        if errorlevel 1 goto fail
    )
)

echo.
echo Fichiers qui seront pris en compte:
git status --short

echo.
git add -A
if errorlevel 1 goto fail

git diff --cached --quiet
if errorlevel 2 goto fail
if not errorlevel 1 (
    echo Aucun changement a committer.
    echo Push de la branche %BRANCH%...
    git push -u origin "%BRANCH%"
    if errorlevel 1 goto fail
    goto done
)

echo.
set /p "COMMIT_MSG=Message de commit [Update project]: "
if not defined COMMIT_MSG set "COMMIT_MSG=Update project"

git commit -m "%COMMIT_MSG%"
if errorlevel 1 goto fail

echo.
echo Push vers GitHub...
git push -u origin "%BRANCH%"
if errorlevel 1 goto fail

:done
echo.
echo Termine.
pause
exit /b 0

:fail
echo.
echo Une erreur est survenue. Verifie le message ci-dessus.
pause
exit /b 1

:ensure_ignore
set "IGNORE_PATTERN=%~1"
if not exist ".gitignore" type nul > ".gitignore"
findstr /l /x /c:"%IGNORE_PATTERN%" ".gitignore" >nul 2>nul
if errorlevel 1 echo %IGNORE_PATTERN%>>".gitignore"
exit /b 0
