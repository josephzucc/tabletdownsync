o="actividad_matthiasstuermer_20261101_112506.csv";
de=$(echo "$o" | sed "s/\.csv/_deu\.csv/g" );
en=$(echo "$o" | sed "s/\.csv/_eng\.csv/g" );
echo -e "$de ==> $en";
csvsql --tables t --query "SELECT texto FROM t" $o >>${de};
python3 traducir.py "$de" "$en"
