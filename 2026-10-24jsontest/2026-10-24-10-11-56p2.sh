program=$(find . -maxdepth 1 -type f -iname "*${1}*.py" 2>/dev/null | sort | tail -n 1);
json=$(python3 ${program}) || exit 1

name=$(jq -r '.name' <<< "$json")
count=$(jq -r '.count' <<< "$json")

printf 'Name: %s, count: %s\n' "$name" "$count"
