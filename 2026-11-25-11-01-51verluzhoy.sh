source "$functest";gn;
a=$(pwd);
folderluz=$(find $ds -type d -iname "*avilalaadradatravesia*" 2>/dev/null | sort | tail -n 1 );
cd "$folderluz";
bash *show*.sh ${today};
cd "$a";
