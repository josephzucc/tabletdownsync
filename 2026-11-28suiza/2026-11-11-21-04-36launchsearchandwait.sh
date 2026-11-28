source "2026-11-11-18-56-05searchall01.sh";
waitswiss () { while read -r line;do s=$(( ( RANDOM % 10 ) + 1 ));sleep $s;wait;echo $line;done  }
d=$(mktemp);
cat *searchterms02* >>$d;
bash *filtertermsenglish.sh >>$d;
cat $d | waitswiss | searchswiss;rm $d
