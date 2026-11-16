while (( 1 == 1));do source ../functest.sh;getNow;
echo $now;
sleep 120;
wait;
python3 ebib.py -acc todas -resumen
done
