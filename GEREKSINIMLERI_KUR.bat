@echo off
setlocal
chcp 65001 >nul
title Ankara Metro Kamera Kontrol - Kurulum Asistanı

echo =====================================================================
echo    ANKARA METRO KAMERA KONTROL - HIZLI KURULUM ASİSTANI
echo =====================================================================
echo.

:: Python kontrolu
where python >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_EXE=python"
) else (
    where py >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=py"
    ) else (
        echo [HATA] Sisteminizde Python bulunamadi!
        echo Lutfen once Python 3.8 veya uzeri bir surumu kurun.
        echo(
        pause
        exit /b 1
    )
)

cd /d "%~dp0"

echo Lutfen yapmak istediginiz islemi secin:
echo.
echo  [1] Hizli Kurulum (Otomatik - Varsa yerel paketlerden, yoksa internetten kur)
echo  [2] Cevrimdisi Paketleri Indir (Internetli PC'de USB'ye aktarmak icin indirir)
echo  [3] Sadece Cevrimdisi Kurulum (kurulum_paketleri klasorunden internetsiz kur)
echo  [4] Kurulum Durumunu Test Et (OpenCV ve PyQt5 kontrolu)
echo  [5] Cikis
echo.

set /p SECIM="Seciminiz [1-5]: "

if "%SECIM%"=="1" goto OTO_KURULUM
if "%SECIM%"=="2" goto PAKET_INDIR
if "%SECIM%"=="3" goto CEVRIMDISI_KUR
if "%SECIM%"=="4" goto TEST_ET
if "%SECIM%"=="5" exit /b 0

echo Gecersiz secim!
pause
exit /b 1

:OTO_KURULUM
echo.
echo [ISLEM] Gereksinimler kuruluyor...
if exist "%~dp0kurulum_paketleri" (
    echo [BILGI] Yerel kurulum_paketleri klasoru bulundu, cevirimdisi kuruluyor...
    "%PYTHON_EXE%" -m pip install --no-index --find-links="%~dp0kurulum_paketleri" -r requirements.txt
) else (
    echo [BILGI] Internet uzerinden kuruluyor...
    "%PYTHON_EXE%" -m pip install -r requirements.txt
)
goto BITIR

:PAKET_INDIR
echo.
echo [ISLEM] Cevrimdisi kurulum paketleri indiriliyor (Internet gereklidir)...
if not exist "%~dp0kurulum_paketleri" mkdir "%~dp0kurulum_paketleri"
"%PYTHON_EXE%" -m pip download -r requirements.txt -d "%~dp0kurulum_paketleri"
echo.
echo [BASARILI] Paketler "%~dp0kurulum_paketleri" klasorune indirildi!
echo Bu klasoru cevrimdisi sisteme tasiyarak 3. secenekle internetsiz kurabilirsiniz.
goto BITIR

:CEVRIMDISI_KUR
echo.
echo [ISLEM] Cevrimdisi kurulum baslatiliyor (kurulum_paketleri klasoru)...
if not exist "%~dp0kurulum_paketleri" (
    echo [HATA] "%~dp0kurulum_paketleri" klasoru bulunamadi!
    echo Lutfen once internetli bir bilgisayarda 2. secenekle paketleri indirin.
    pause
    exit /b 1
)
"%PYTHON_EXE%" -m pip install --no-index --find-links="%~dp0kurulum_paketleri" -r requirements.txt
goto BITIR

:TEST_ET
echo.
echo [ISLEM] Sistem gereksinimleri test ediliyor...
"%PYTHON_EXE%" -c "import cv2; print(' [OK] OpenCV Versiyonu:', cv2.__version__)" 2>nul
if errorlevel 1 (
    echo  [EKSIK] OpenCV (cv2) yuklu degil!
) else (
    echo  [OK] OpenCV basariyla yuklu.
)

"%PYTHON_EXE%" -c "from PyQt5 import QtWidgets; print(' [OK] PyQt5 Arayuz Motoru Hazir')" 2>nul
if errorlevel 1 (
    echo  [EKSIK] PyQt5 yuklu degil!
) else (
    echo  [OK] PyQt5 basariyla yuklu.
)
goto BITIR

:BITIR
echo.
echo =====================================================================
echo Islem tamamlandi.
echo =====================================================================
echo Devam etmek icin bir tusa basin...
pause >nul
