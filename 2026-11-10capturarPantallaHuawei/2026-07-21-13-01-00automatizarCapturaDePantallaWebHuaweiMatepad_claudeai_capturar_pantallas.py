#!/usr/bin/env python3
"""
capturar_pantallas.py
======================

Recorre una pagina web haciendo scroll hacia abajo y guarda una captura
de pantalla de cada "pantalla" (viewport) hasta llegar al final del
contenido, o hasta un limite de seguridad.

------------------------------------------------------------------
OPCION A - Todo en la MatePad, dentro de Termux (Chromium via TUR)
------------------------------------------------------------------
    pkg update && pkg upgrade -y
    pkg install python x11-repo tur-repo -y
    pkg install chromium -y
    pip install "selenium==4.9.1"

    termux-setup-storage        # una sola vez, para poder guardar en ~/storage

    python capturar_pantallas.py "https://ejemplo.com"

NOTA: en Termux, Selenium 4.9.1 es la version mas reciente que la
comunidad reporta funcionando bien con el Chromium empaquetado en TUR.
Versiones mas nuevas de Selenium intentan auto-gestionar el driver de
una forma que no siempre encaja con Android/Termux.

------------------------------------------------------------------
OPCION B - Usando un Chrome remoto (ej. Docker en tu iMac / VM Ubuntu)
------------------------------------------------------------------
En el iMac o la VM de Ubuntu (donde ya tienes Docker funcionando con Ollama):

    docker run -d -p 4444:4444 --shm-size=2g selenium/standalone-chrome

Y desde la tablet, sin necesidad de Chromium ni TUR (solo "pip install selenium"):

    python capturar_pantallas.py "https://ejemplo.com" --remote http://IP_DEL_IMAC:4444

Si el modo --remote da error de conexion, prueba anadiendo /wd/hub al
final de la URL (depende de la version de Selenium Grid usada).
------------------------------------------------------------------
"""

import argparse
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


def crear_driver(remote_url, ancho, alto):
    opciones = Options()
    opciones.add_argument("--headless=new")
    opciones.add_argument("--no-sandbox")
    opciones.add_argument("--disable-dev-shm-usage")
    opciones.add_argument(f"--window-size={ancho},{alto}")

    if remote_url:
        return webdriver.Remote(command_executor=remote_url, options=opciones)
    return webdriver.Chrome(options=opciones)


def altura_total_pagina(driver):
    return driver.execute_script(
        "return Math.max(document.body.scrollHeight, "
        "document.documentElement.scrollHeight);"
    )


def capturar_pantallas(url, carpeta_salida, remote_url=None,
                        espera=1.5, ancho=1366, alto=900, max_capturas=200):
    carpeta = Path(carpeta_salida).expanduser()
    carpeta.mkdir(parents=True, exist_ok=True)

    driver = crear_driver(remote_url, ancho, alto)
    try:
        driver.get(url)
        time.sleep(espera * 2)  # margen extra para la primera carga

        posicion = 0
        num = 1

        while True:
            driver.execute_script(f"window.scrollTo(0, {posicion});")
            time.sleep(espera)  # deja tiempo a contenido de carga perezosa (lazy-load)

            ruta = carpeta / f"pantalla_{num:03d}.png"
            driver.save_screenshot(str(ruta))
            print(f"[{num}] guardada -> {ruta}")

            total = altura_total_pagina(driver)
            siguiente = min(posicion + alto, max(0, total - alto))

            if siguiente <= posicion or num >= max_capturas:
                print(f"Fin de la pagina. Total de capturas: {num}")
                break

            posicion = siguiente
            num += 1
    finally:
        driver.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Capturas de pantalla en scroll de una pagina web")
    parser.add_argument("url", help="URL de la pagina a capturar")
    parser.add_argument("--salida", default="~/capturas_web", help="Carpeta de salida")
    parser.add_argument("--remote", default=None,
                         help="URL de un Selenium remoto, ej. http://192.168.1.50:4444")
    parser.add_argument("--espera", type=float, default=1.5,
                         help="Segundos de espera tras cada scroll (sube este valor si la pagina carga contenido lento)")
    parser.add_argument("--ancho", type=int, default=1366, help="Ancho del viewport")
    parser.add_argument("--alto", type=int, default=900, help="Alto del viewport (= alto de cada captura)")
    parser.add_argument("--max", type=int, default=200, help="Limite de seguridad de capturas")
    args = parser.parse_args()

    capturar_pantallas(
        args.url, args.salida,
        remote_url=args.remote, espera=args.espera,
        ancho=args.ancho, alto=args.alto, max_capturas=args.max,
    )
