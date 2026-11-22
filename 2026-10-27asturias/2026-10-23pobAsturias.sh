if (( $# > 0 ));then limit="LIMIT ${1}";fi;
source "$shar/Download/DownloadSync/functest.sh" >/dev/null;
gn;
d="${now}lista.txt";
csvsql -d ';' -I -e latin-1 --tables t --query "SELECT * FROM (SELECT SUBSTR(Municipios,7) AS MUNICIPIOS, CAST(REPLACE(Total,'.','') AS INTEGER ) as TOTAL FROM t) AS t ORDER BY TOTAL DESC ${limit}" poblacionAsturias.csv | csvlook -d ',';
rm $d 2>/dev/null
