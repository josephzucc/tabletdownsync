echo "Cambia pestana_20260927_1323.txt por 2026-09-27-13-23-00_pestanas.txt"
ano=$(date +"%Y");ls *pestana_${ano}* | while read -r file;do mv -- $file ${file:9:4}-${file:13:2}-${file:15:2}-${file:18:2}-${file:20:2}-00_pestanas.txt ;done
