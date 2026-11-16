#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, render_template_string, send_file

APP_DIR = Path.home() / "carrusel"
PID_FILE = APP_DIR / "carrusel.pid"
LOG_FILE = APP_DIR / "server.log"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
    ".bmp", ".tif", ".tiff", ".avif"
}

app = Flask(__name__)

IMAGE_FOLDER = Path.cwd()
IMAGE_FILES = []


def ensure_app_dir():
    APP_DIR.mkdir(parents=True, exist_ok=True)


def load_images():
    global IMAGE_FILES

    if not IMAGE_FOLDER.is_dir():
        IMAGE_FILES = []
        return

    files = []
    try:
        for entry in IMAGE_FOLDER.iterdir():
            if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS:
                files.append(entry)
    except OSError:
        IMAGE_FILES = []
        return

    files.sort(key=lambda p: p.name.casefold())
    IMAGE_FILES = files


def read_pid():
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def process_exists(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def write_pid():
    ensure_app_dir()
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def remove_pid():
    try:
        PID_FILE.unlink()
    except (FileNotFoundError, OSError):
        pass


def wake_lock():
    try:
        subprocess.run(
            ["termux-wake-lock"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


def wake_unlock():
    try:
        subprocess.run(
            ["termux-wake-unlock"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


def heartbeat():
    while True:
        time.sleep(60)


def stop_server():
    pid = read_pid()

    if pid is None:
        print("No hay un servidor registrado.")
        return 0

    if not process_exists(pid):
        remove_pid()
        print("El servidor no estaba ejecutándose.")
        return 0

    print(f"Deteniendo servidor PID {pid}...")

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pid()
        print("Servidor detenido.")
        return 0
    except OSError as exc:
        print(f"Error al detener el servidor: {exc}")
        return 1

    for _ in range(30):
        time.sleep(0.1)
        if not process_exists(pid):
            remove_pid()
            print("Servidor detenido.")
            return 0

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        print(f"Error al forzar la detención: {exc}")
        return 1

    remove_pid()
    print("Servidor detenido forzosamente.")
    return 0


def server_status():
    pid = read_pid()

    if pid is not None and process_exists(pid):
        print(f"Servidor EJECUTÁNDOSE. PID: {pid}")
        print(f"URL: http://127.0.0.1:{DEFAULT_PORT}")
        print(f"Log: {LOG_FILE}")
        print(f"Carpeta: {IMAGE_FOLDER}")
        return 0

    if pid is not None:
        remove_pid()

    print("Servidor NO está ejecutándose.")
    return 1


@app.get("/")
def index():
    load_images()

    image_data = []

    for path in IMAGE_FILES:
        try:
            mtime = int(path.stat().st_mtime_ns)
        except OSError:
            mtime = 0

        image_data.append({
            "name": path.name,
            "mtime": mtime,
        })

    return render_template_string(
        HTML,
        images_json=json.dumps(
            image_data,
            ensure_ascii=False
        ),
    )


@app.get("/image/<int:index>")
def image(index):
    load_images()

    if index < 0 or index >= len(IMAGE_FILES):
        abort(404)

    path = IMAGE_FILES[index]

    if not path.is_file():
        abort(404)

    response = send_file(
        path,
        conditional=True,
        max_age=0,
    )

    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate, max-age=0"
    )
    response.headers["Pragma"] = "no-cache"

    return response


@app.get("/health")
def health():
    load_images()

    return jsonify({
        "status": "ok",
        "folder": str(IMAGE_FOLDER),
        "images": len(IMAGE_FILES),
        "pid": os.getpid(),
    })


HTML = r"""
<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1,
               viewport-fit=cover">
<title>Carrusel</title>

<style>
* {
    box-sizing: border-box;
}

html,
body {
    margin: 0;
    padding: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: #000;
    color: #fff;
    font-family: sans-serif;
}

body {
    height: 100dvh;
}

#app {
    position: relative;
    width: 100%;
    height: 100dvh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #000;
    overflow: hidden;
}

#mainImage {
    display: block;
    max-width: 100%;
    max-height: 100%;
    width: auto;
    height: auto;
    object-fit: contain;
    user-select: none;
    -webkit-user-drag: none;
    touch-action: pan-y;
}

#info {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    padding: 10px 12px;
    padding-bottom: max(10px, env(safe-area-inset-bottom));
    background: linear-gradient(
        transparent,
        rgba(0, 0, 0, 0.85)
    );
    text-align: center;
    pointer-events: none;
}

#filename {
    font-size: 15px;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
}

#counter {
    margin-top: 3px;
    font-size: 13px;
    opacity: 0.75;
}

#controls {
    position: absolute;
    top: max(10px, env(safe-area-inset-top));
    left: 0;
    right: 0;
    display: flex;
    justify-content: center;
    gap: 8px;
    padding: 8px;
    opacity: 0;
    transition: opacity 0.25s;
}

#app:hover #controls,
#controls.visible {
    opacity: 1;
}

button {
    border: 0;
    border-radius: 8px;
    padding: 9px 13px;
    background: rgba(40, 40, 40, 0.85);
    color: #fff;
    font-size: 15px;
    cursor: pointer;
}

button:active {
    background: rgba(100, 100, 100, 0.9);
}

#message {
    text-align: center;
    padding: 20px;
    color: #ccc;
}
</style>
</head>

<body>

<div id="app">

    <img id="mainImage" alt="">

    <div id="message" hidden></div>

    <div id="controls">
        <button id="prev" type="button">◀</button>
        <button id="play" type="button">▶</button>
        <button id="next" type="button">▶</button>
        <button id="fullscreen" type="button">⛶</button>
    </div>

    <div id="info">
        <div id="filename"></div>
        <div id="counter"></div>
    </div>

</div>

<script>
"use strict";

const images = {{ images_json|safe }};

const mainImage =
    document.getElementById("mainImage");

const filenameElement =
    document.getElementById("filename");

const counterElement =
    document.getElementById("counter");

const messageElement =
    document.getElementById("message");

const controls =
    document.getElementById("controls");

const prevButton =
    document.getElementById("prev");

const playButton =
    document.getElementById("play");

const nextButton =
    document.getElementById("next");

const fullscreenButton =
    document.getElementById("fullscreen");

let currentIndex = 0;
let playing = false;
let slideshowTimer = null;

/*
 * No utilizamos una variable "loading".
 * Cada cambio recibe un número de solicitud.
 * Así las flechas siguen funcionando aunque una
 * imagen tarde en cargarse.
 */
let requestToken = 0;

let touchStartX = null;
let touchStartY = null;


function imageUrl(index) {
    const item = images[index];

    return "/image/" +
        index +
        "?v=" +
        encodeURIComponent(item.mtime);
}


function normalizeIndex(index) {
    if (images.length === 0) {
        return 0;
    }

    return (index + images.length) % images.length;
}


function updateInfo() {
    if (images.length === 0) {
        filenameElement.textContent = "";
        counterElement.textContent = "";
        return;
    }

    filenameElement.textContent =
        images[currentIndex].name;

    counterElement.textContent =
        (currentIndex + 1) +
        " / " +
        images.length;
}


function showImage(index) {
    if (images.length === 0) {
        messageElement.hidden = false;
        messageElement.textContent =
            "No se han encontrado imágenes.";
        return;
    }

    messageElement.hidden = true;

    currentIndex = normalizeIndex(index);
    updateInfo();

    const token = ++requestToken;
    const url = imageUrl(currentIndex);

    /*
     * Cargamos la nueva imagen aparte.
     * Si el usuario pulsa varias veces rápidamente,
     * solo la última solicitud puede cambiar la imagen.
     */
    const loader = new Image();

    loader.onload = function() {
        if (token !== requestToken) {
            return;
        }

        mainImage.src = url;
        mainImage.alt = images[currentIndex].name;
    };

    loader.onerror = function() {
        if (token !== requestToken) {
            return;
        }

        filenameElement.textContent =
            "Error al cargar: " +
            images[currentIndex].name;
    };

    loader.src = url;
}


function nextImage() {
    if (images.length > 0) {
        showImage(currentIndex + 1);
    }
}


function previousImage() {
    if (images.length > 0) {
        showImage(currentIndex - 1);
    }
}


function updatePlayButton() {
    playButton.textContent =
        playing ? "⏸" : "▶";

    playButton.title =
        playing ? "Pausar" : "Reproducir";
}


function startSlideshow() {
    stopSlideshow();

    if (images.length <= 1) {
        playing = false;
        updatePlayButton();
        return;
    }

    playing = true;
    updatePlayButton();

    slideshowTimer = setInterval(
        function() {
            nextImage();
        },
        5000
    );
}


function stopSlideshow() {
    if (slideshowTimer !== null) {
        clearInterval(slideshowTimer);
        slideshowTimer = null;
    }

    playing = false;
    updatePlayButton();
}


function toggleSlideshow() {
    if (playing) {
        stopSlideshow();
    } else {
        startSlideshow();
    }
}


function toggleFullscreen() {
    const element = document.documentElement;

    if (!document.fullscreenElement) {
        if (element.requestFullscreen) {
            element.requestFullscreen().catch(
                function() {}
            );
        }
    } else {
        if (document.exitFullscreen) {
            document.exitFullscreen().catch(
                function() {}
            );
        }
    }
}


function showControlsTemporarily() {
    controls.classList.add("visible");

    window.clearTimeout(
        showControlsTemporarily.timer
    );

    showControlsTemporarily.timer =
        window.setTimeout(
            function() {
                controls.classList.remove("visible");
            },
            1800
        );
}


prevButton.addEventListener(
    "click",
    function() {
        previousImage();
        showControlsTemporarily();
    }
);


nextButton.addEventListener(
    "click",
    function() {
        nextImage();
        showControlsTemporarily();
    }
);


playButton.addEventListener(
    "click",
    function() {
        toggleSlideshow();
        showControlsTemporarily();
    }
);


fullscreenButton.addEventListener(
    "click",
    function() {
        toggleFullscreen();
        showControlsTemporarily();
    }
);


document.addEventListener(
    "keydown",
    function(event) {

        if (event.key === "ArrowRight") {
            event.preventDefault();
            nextImage();
            showControlsTemporarily();

        } else if (event.key === "ArrowLeft") {
            event.preventDefault();
            previousImage();
            showControlsTemporarily();

        } else if (event.key === " ") {
            event.preventDefault();
            toggleSlideshow();
            showControlsTemporarily();

        } else if (
            event.key === "f" ||
            event.key === "F"
        ) {
            event.preventDefault();
            toggleFullscreen();
            showControlsTemporarily();

        } else if (event.key === "Escape") {
            controls.classList.remove("visible");
        }
    }
);


document.addEventListener(
    "touchstart",
    function(event) {

        if (event.touches.length !== 1) {
            return;
        }

        touchStartX =
            event.touches[0].clientX;

        touchStartY =
            event.touches[0].clientY;
    },
    {passive: true}
);


document.addEventListener(
    "touchend",
    function(event) {

        if (
            touchStartX === null ||
            touchStartY === null
        ) {
            return;
        }

        const endX =
            event.changedTouches[0].clientX;

        const endY =
            event.changedTouches[0].clientY;

        const dx = endX - touchStartX;
        const dy = endY - touchStartY;

        touchStartX = null;
        touchStartY = null;

        if (Math.abs(dx) < 50) {
            return;
        }

        if (Math.abs(dx) <= Math.abs(dy)) {
            return;
        }

        if (dx < 0) {
            nextImage();
        } else {
            previousImage();
        }

        showControlsTemporarily();
    }
);


if (images.length > 0) {
    showImage(0);
} else {
    messageElement.hidden = false;
    messageElement.textContent =
        "No se han encontrado imágenes.";
    updateInfo();
}

updatePlayButton();

</script>

</body>
</html>
"""


def run_server(host, port):
    ensure_app_dir()
    write_pid()
    wake_lock()

    def cleanup(signum=None, frame=None):
        remove_pid()
        wake_unlock()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    load_images()

    print(f"Servidor: http://{host}:{port}")
    print(f"Carpeta: {IMAGE_FOLDER}")
    print(f"Imágenes: {len(IMAGE_FILES)}")

    threading.Thread(
        target=heartbeat,
        daemon=True
    ).start()

    try:
        from waitress import serve

        print("Servidor HTTP: Waitress")

        serve(
            app,
            host=host,
            port=port,
            threads=8,
            connection_limit=50,
            channel_timeout=120,
        )

    except ImportError:

        print(
            "Waitress no está instalado; "
            "usando Flask con múltiples hilos."
        )

        app.run(
            host=host,
            port=port,
            threaded=True,
            debug=False,
            use_reloader=False,
        )

    finally:
        remove_pid()
        wake_unlock()


def start_background(host, port, folder):
    ensure_app_dir()

    existing_pid = read_pid()

    if (
        existing_pid is not None and
        process_exists(existing_pid)
    ):
        print(
            f"El servidor ya está ejecutándose. "
            f"PID: {existing_pid}"
        )
        print(f"http://{host}:{port}")
        return 0

    if existing_pid is not None:
        remove_pid()

    script_path = Path(__file__).resolve()

    log_handle = open(
        LOG_FILE,
        "a",
        encoding="utf-8",
        buffering=1,
    )

    command = [
        sys.executable,
        str(script_path),
        "--folder",
        str(folder),
        "--host",
        host,
        "--port",
        str(port),
    ]

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        log_handle.close()
        raise

    log_handle.close()

    print(
        f"Servidor iniciado en segundo plano. "
        f"PID inicial: {process.pid}"
    )
    print(f"http://{host}:{port}")
    print(f"Log: {LOG_FILE}")

    time.sleep(1)

    pid = read_pid()

    if pid is not None and process_exists(pid):
        print(f"Servidor activo. PID: {pid}")
        return 0

    if process.poll() is not None:
        print(
            "El proceso terminó al arrancar. "
            f"Revisa: {LOG_FILE}"
        )
        return 1

    print(
        "El servidor está arrancando. "
        f"Revisa: {LOG_FILE}"
    )
    return 0


def main():
    global IMAGE_FOLDER

    parser = argparse.ArgumentParser(
        description="Carrusel de imágenes para Termux/Firefox."
    )

    parser.add_argument(
        "--folder",
        default=str(Path.cwd()),
        help="Carpeta que contiene las imágenes.",
    )

    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Dirección de escucha.",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Puerto del servidor.",
    )

    parser.add_argument(
        "--background",
        action="store_true",
        help="Ejecutar en segundo plano.",
    )

    parser.add_argument(
        "--stop",
        action="store_true",
        help="Detener el servidor.",
    )

    parser.add_argument(
        "--status",
        action="store_true",
        help="Mostrar el estado.",
    )

    args = parser.parse_args()

    if args.stop:
        return stop_server()

    if args.status:
        return server_status()

    folder = Path(args.folder).expanduser()

    try:
        folder = folder.resolve()
    except OSError:
        folder = folder.absolute()

    if not folder.is_dir():
        print(
            "ERROR: la carpeta no existe o no es una carpeta:"
        )
        print(folder)
        return 1

    IMAGE_FOLDER = folder

    if args.background:
        return start_background(
            args.host,
            args.port,
            IMAGE_FOLDER,
        )

    run_server(
        args.host,
        args.port,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
