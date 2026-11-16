d=$(ls ~/${today}histvar.txt);
if (( $# > 0 ));then
  csvsql -H -d ';' --tables v --query "SELECT a, b FROM v WHERE a LIKE '%${1}%' LIMIT 1" $d | csvlook -d ',' --max-column-width 95;
else echo "No se ha especificado variables";
fi;
