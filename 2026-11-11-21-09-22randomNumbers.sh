echo "randoms numbers between 1 and 10";
i=1;while (( i < 100 ));do echo $(( ( RANDOM % 10 ) + 1 ));i=$((i+1));done
