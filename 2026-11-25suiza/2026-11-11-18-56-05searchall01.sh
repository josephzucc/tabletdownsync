searchswiss () {
while read -r term;do
  python3 foroleer.py reddit "${term}" --subreddit Switzerland --subreddit askswitzerland;
  python3 foroleer.py quora "${term}";
  python3 foroleer.py swissforum "${term}";
done;
}
