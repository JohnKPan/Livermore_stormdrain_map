@echo off
REM ---------------------------------------------------------------------------
REM Build every registered city, one after another.
REM
REM   run_all_cities.bat                       full rebuild of each
REM   run_all_cities.bat --render-only         pages and overview only
REM   run_all_cities.bat --jobs 4              anything else passes straight
REM                                            through to run_pipeline.py
REM
REM SEQUENTIALLY, and that is the point. The page build already spreads streets
REM across cores, so two cities at once is no faster -- and each worker holds a
REM FULL copy of that city's corpus, so running two eight-worker builds together
REM needs about 50 GB of workers against 24 GB free. San Jose had to be killed
REM and restarted at --jobs 3 for exactly this reason.
REM
REM A .bat rather than a shell script: cmd is the one interpreter on this
REM machine that is not ambiguous. `bash` resolves to WSL from PowerShell, and a
REM WSL bash configuring a Windows python is what produced a whole afternoon of
REM "GDAL_DATA is not defined". run_pipeline.py is stdlib-only and re-execs the
REM conda interpreter itself, so this file only has to start it.
REM
REM The city list comes from `run_pipeline.py --list-cities`, which reads
REM DEM_PROJECTS. Adding a city there is enough; there is no second list here to
REM forget to update.
REM ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY=%~dp0.conda\env\python.exe"
if not exist "%PY%" (
    echo run_all_cities.bat: no interpreter at "%PY%"
    echo   create it:  .conda\micromamba.exe create -y -p .conda\env -f environment.yml
    exit /b 1
)

set "FAILED="
set "DONE="
set /a NCITY=0

echo.
echo === cities to build ===
for /f "usebackq delims=" %%c in (`"%PY%" run_pipeline.py --list-cities`) do (
    echo    %%c
    set /a NCITY+=1
)
if %NCITY%==0 (
    echo run_all_cities.bat: no cities registered in DEM_PROJECTS.
    exit /b 1
)
echo.
echo Extra arguments passed to every city: %*
echo.

for /f "usebackq delims=" %%c in (`"%PY%" run_pipeline.py --list-cities`) do (
    echo.
    echo ============================================================
    echo ==  %%c
    echo ============================================================
    "%PY%" run_pipeline.py --city %%c %*
    if errorlevel 1 (
        echo.
        echo !! %%c FAILED -- continuing with the rest
        set "FAILED=!FAILED! %%c"
    ) else (
        set "DONE=!DONE! %%c"
    )
)

echo.
echo ============================================================
echo ==  summary
echo ============================================================
echo   built :!DONE!
if defined FAILED (
    echo   FAILED:!FAILED!
    echo.
    echo One or more cities did not finish. Their output is incomplete --
    echo rebuild those individually before publishing anything.
    exit /b 1
)
echo   all cities completed
exit /b 0
