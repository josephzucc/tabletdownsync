source ../functest.sh
getNow;getToday;
echo "Esperamos 10 segundos pid ${pid} ${now}";
sleep 10;
echo "Mostrando ${today}*bashtest2_mensajes.txt" 
cat "../${today}*bashtest2_mensajes.txt"
wait;
getNow;
echo "Hemos vuelto ${pid} ${now}";
