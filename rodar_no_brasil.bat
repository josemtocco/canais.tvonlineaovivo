@echo off
chcp 65001 >nul
title TV Online Ao Vivo - Gerador de Lista M3U
color 0B

cd /d "%~dp0"

echo ==================================================================
echo   TV ONLINE AO VIVO  -  GERADOR DE LISTA M3U PARA SS IPTV
echo ==================================================================
echo.
echo  Este programa vai:
echo    1. Ler todas as categorias do site
echo    2. Coletar todos os canais (inclusive os do botao "Carregar mais")
echo    3. Testar se cada canal esta realmente tocando
echo    4. Gerar o arquivo tvonlineaovivo.m3u
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] O Python nao foi encontrado neste computador.
    echo.
    echo  Baixe em: https://www.python.org/downloads/
    echo  IMPORTANTE: marque a opcao "Add python.exe to PATH" na instalacao.
    echo.
    pause
    exit /b 1
)

echo Python encontrado:
python --version
echo.
echo Iniciando... isso pode levar de 3 a 6 minutos.
echo.

python gerar_m3u.py

if errorlevel 1 (
    echo.
    echo [AVISO] A execucao terminou com erros. Veja as mensagens acima.
) else (
    echo.
    echo ==================================================================
    echo   PRONTO! Arquivo gerado: tvonlineaovivo.m3u
    echo ==================================================================
]

echo.
pause
