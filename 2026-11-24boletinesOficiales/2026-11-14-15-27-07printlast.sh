printlast () {
 l=$(tail -n 1);
 echo $l;
 cat "$l" | more;
}
