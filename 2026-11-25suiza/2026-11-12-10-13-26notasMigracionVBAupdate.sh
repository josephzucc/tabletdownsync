update () { d="${dest}";while read -r line;do echo "done;${line}";done };
o="2026-11-12-10-13-26notasMigracionVBAbbdd.sh";
item="replace Access database open source";
orig="2026-11-12-10-13-26notasMigracionVBAtask02.txt";
dest="2026-11-12-10-13-26notasMigracionVBAtask03.txt"
csvsql --tables t --no-header-row --query "SELECT b FROM t WHERE b == \"${item}\"" ${orig} | tail -n +2 | update >>${dest};
csvsql --tables t --no-header-row --query "SELECT a,b FROM t WHERE b <> \"${item}\"" ${orig} | sed "s/notdone,/notdone;/g" | tail -n +2 >>${dest};
cat ${dest}
