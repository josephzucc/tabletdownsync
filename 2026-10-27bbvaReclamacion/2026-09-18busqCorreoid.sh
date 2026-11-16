echo "python3 leerCorreo.py -acc gmx yahoo -descargar ${1} -ver-txt ${1}"
python3 -c "import sys; print('interactivo:', sys.stdin.isatty())"
python3 leerCorreo.py -acc gmx yahoo -descargar ${1} -ver-txt ${1}
