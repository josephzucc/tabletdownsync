gn;d="${now}selist.sh";echo "source \"${functest}\"" >>$d;echo -e $selist | while read -r se;do echo "se ${se}" >>$d;done;cat $d;bash $d;rm $d
