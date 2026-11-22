source "$functest";
o=$(ls 2026-11-12-10-13-26notasMigracionVBA*task02*.*);
item=$(csvsql --tables t --no-header-row --query "SELECT b FROM t WHERE a == \"notdone\" LIMIT 1" $o | tail -n +2);
gn;echo "${now}: Buscando $item";
python3 foroleer.py reddit "${item}" --subreddit Switzerland --subreddit askswitzerland;
gn;
echo "${now}:Terminado busqueda ${item}"
