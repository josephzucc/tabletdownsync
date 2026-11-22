echo -e "En \$a esta el texto completo\nEn \$b esta el texto buscado\nY devuelve el numero de indice con base 1, donde esta el texto buscado\nSi no lo encuentra devuelve -1"
a="bigs cat blue";
b="cat";
echo -e "Texto completo :\t$a\nTexto Buscado :\t\t$b"
x="${a%%"$b"*}";
echo -n -e "Resultado :\t\t"
[[ "$x" = "$a" ]] && echo -1 || echo "${#x}"
