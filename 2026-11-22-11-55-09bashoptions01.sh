while getopts "fghij" opt; do
  case $opt in
    f)
      echo "Flag -f was set"
      ;;
    g)
      echo "Flag -g was set"
      ;;
    h)
      echo "Flag -h was set"
      ;;
    i)
      echo "Flag -i was set"
      ;;
    j)
      echo "Flag -j was set"
      ;;
    \?)
      echo "Invalid option: -$OPTARG" >&2
      exit 1
      ;;
  esac
done

# Shift the positional parameters so that $1 is the first non-option argument
shift $((OPTIND - 1))
