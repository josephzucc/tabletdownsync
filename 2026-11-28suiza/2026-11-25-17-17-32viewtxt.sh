m2 () { while read -r file;do echo "cat \"$file\" | less -M" >>$d ;done  };d=$(mktemp);bash *filtrartxtsuiza.sh | sort -r | m2;bash $d;rm $d
