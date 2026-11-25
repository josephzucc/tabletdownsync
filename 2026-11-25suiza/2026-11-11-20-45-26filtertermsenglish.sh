d=$(mktemp);bash *filterterms* >>${d} ;csvsql --tables t --no-header-row --query "SELECT b FROM t WHERE a == \"eng\"" $d | tail -n +2;rm $d
