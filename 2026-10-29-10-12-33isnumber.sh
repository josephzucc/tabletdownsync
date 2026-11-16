read -p " var ? " v;
if [[ $v =~ ^[0-9]+$ ]];
  then echo "It is a number"
  else echo "It is not a number";
fi
