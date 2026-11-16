# Define a cleanup function
cleanup() {
    echo "Caught signal, cleaning up temporary files..."
    rm -f /tmp/my_temp_file
    exit 0
}

# Trap SIGTERM (signal 15) and SIGINT (signal 2)
trap cleanup SIGTERM SIGINT

# Your actual background work
while true; do
    echo "Process is running in background..."
    sleep 10
done
