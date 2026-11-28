#echo "Envio ruta de traducir.py por stdout";
find $ds -type f -iname "*traducir*.py" 2>/dev/null | sort | tail -n 1;
