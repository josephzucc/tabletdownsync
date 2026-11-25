pags="";
case $# in
1) doc=${1};echo "Documento $doc";;
2) doc=${1};pg1=${2};pags=" -f ${pg1} -l ${pg1} ";echo "Documento $doc pagina $pg1";;
3) doc=${1};pg1=${2};pg2=${3};pags=" -f ${pg1} -l ${pg2} ";echo "Documento $doc pagina ${pg1}-${pg2}";;
esac;
ex="pdftoppm \${pags} -r 200 -png \${doc} \${doc}_page";
eval "$ex";
