echo "Busco histvar.txt ( 0=baja )";
select file in $( find . -type f -iname "*histvar.txt" -printf "%P\n" 2>/dev/null );do (( $REPLY == 0 )) && break || rm "$file";done
echo "Busco correos.txt ( 0=baja )";
select file in $( find . -type f -iname "*correos.txt" -printf "%P\n" 2>/dev/null );do (( $REPLY == 0 )) && break || rm "$file";done
echo "Busco baja.txt ( 0=baja )";
select file in $( find . -type f -iname "*baja.txt" -printf "%P\n" 2>/dev/null );do (( $REPLY == 0 )) && break || rm "$file";done
echo "Busco lista1.txt ( 0=baja )";
select file in $( find . -type f -iname "*lista1.txt" -printf "%P\n" 2>/dev/null );do (( $REPLY == 0 )) && break || rm "$file";done
