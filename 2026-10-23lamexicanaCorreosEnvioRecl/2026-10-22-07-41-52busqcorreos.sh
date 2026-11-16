source ../functest.sh >/dev/null;
gn;d="${now}lista.txt";ls *INBOX* | cut -c '27-38' >$d;sort -o $d -u $d;while read -r id;do echo "Descargando $id....";python3 $mailapp -acc gmx yahoo -descargar $id -adjuntos -aqui -ds -si-existe sobrescribir;done < <(cat $d);rm $d
