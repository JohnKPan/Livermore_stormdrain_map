@echo off
REM ---------------------------------------------------------------------------
REM Build every city in sequence, each with its OWN spacing and smoothing.
REM
REM   run_all_cities_tuned.bat                  full rebuild, per-city settings
REM   run_all_cities_tuned.bat --render-only    pages and overview only
REM   run_all_cities_tuned.bat --skip-fetch     anything else is appended to
REM                                             EVERY city, after its own flags
REM
REM Why this exists next to run_all_cities.bat: that one appends %* identically
REM to every city, so it can only ever build them all the same way. The settings
REM below differ per city, which is the whole point --
REM
REM   livermore    defaults: 0.15 m corpus, three page corpora (25 10 5)
REM   pleasanton   same
REM   hayward      0.15 m corpus, 10 m window only
REM   fremont      same
REM   san_jose     0.3 m corpus, 10 m window only, and --jobs 3
REM
REM The 10 m window is not an arbitrary pick: run_pipeline.OPENS_AT is 10, so a
REM city built without it would leave the overview opening on a corpus that was
REM never rendered. Nothing validates that, so keep 10 in every --smooth list.
REM
REM --jobs 3 for San Jose is a MEMORY limit, not a speed one. The default is
REM cores-2 (18 here), each worker holds a full copy of the corpus, and San
REM Jose's ran ~10 GB apiece -- eight of them exhausted 64 GB and the build had
REM to be killed. 0.3 m halves the corpus, so 3 is conservative rather than
REM tight; raise it only with the memory free to back it.
REM
REM SEQUENTIALLY, and each city is waited on before the next starts: cmd blocks
REM on a synchronous call, so there is nothing to poll. A city that fails does
REM not stop the run -- the rest still build and the summary names the casualty,
REM because a five-city rebuild is long enough that losing all of it to one bad
REM city is worse than finishing and rerunning that one.
REM
REM A .bat rather than a shell script, for the same reason as run_all_cities.bat:
REM cmd is the one unambiguous interpreter on this machine. `bash` resolves to
REM WSL from PowerShell, and a WSL bash configuring a Windows python is what
REM produced an afternoon of "GDAL_DATA is not defined".
REM ---------------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY=%~dp0.conda\env\python.exe"
if not exist "%PY%" (
    echo run_all_cities_tuned.bat: no interpreter at "%PY%"
    echo   create it:  .conda\micromamba.exe create -y -p .conda\env -f environment.yml
    exit /b 1
)

set "EXTRA=%*"
set "FAILED="
set "DONE="

echo.
echo === build plan ===
echo    livermore    ^(defaults: 0.15 m, smooth 25 10 5^)
echo    pleasanton   ^(defaults: 0.15 m, smooth 25 10 5^)
echo    hayward      --smooth 10
echo    fremont      --smooth 10
echo    san_jose     --spacing 0.3 --smooth 10 --jobs 3
if defined EXTRA echo    appended to every city: %EXTRA%
echo.

call :build livermore
call :build pleasanton
call :build hayward  --smooth 10
call :build fremont  --smooth 10
call :build san_jose --spacing 0.3 --smooth 10 --jobs 3
goto :summary


REM --- :build <city> [flags...] ------------------------------------------------
REM No setlocal here: FAILED and DONE have to survive back to the caller.
:build
set "CITY=%~1"
shift
set "CARGS="
:build_collect
if "%~1"=="" goto build_go
set "CARGS=!CARGS! %~1"
shift
goto build_collect

:build_go
echo.
echo ============================================================
echo ==  %CITY% !CARGS! %EXTRA%
echo ============================================================
"%PY%" run_pipeline.py --city %CITY%!CARGS! %EXTRA%
if errorlevel 1 (
    echo.
    echo !! %CITY% FAILED -- continuing with the rest
    set "FAILED=!FAILED! %CITY%"
) else (
    set "DONE=!DONE! %CITY%"
)
goto :eof


:summary
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
