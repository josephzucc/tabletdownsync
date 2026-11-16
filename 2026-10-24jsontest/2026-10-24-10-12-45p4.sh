program=$(find . -maxdepth 1 -type f -iname "*${1}*.py" 2>/dev/null | sort | tail -n 1);
echo "program $program"
while IFS= read -r item; do
  printf 'Item: %s\n' "$item"
done < <(python3 ${program} | jq -r '.items[]')
