if (( $# == 0 ));then echo "Debe introducir un termino de busqueda de carpetas a comparar";return 1;fi;
difffind () { busq="${1}";d1="";d2="";i=1;while read -r file;do eval "i${i}=\"$file\"";i=$((i+1));done < <( find . -maxdepth 1 -type d -iname "*${busq}*" -printf "%f\n" 2>/dev/null | sort -r | head -n 2 );eval "diff -r $i1 $i2"; } ; difffind ${1} | grep -i -v "only in 2026-11-22"
