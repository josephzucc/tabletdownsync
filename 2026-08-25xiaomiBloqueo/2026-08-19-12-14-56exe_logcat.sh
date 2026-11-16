# Method 1: Basic logcat
logcat -d -t 500 > recent_logs.txt 2>&1

# Method 2: With filtering for boot/reboot errors
logcat -d *:E | grep -i "reboot\|kernel\|panic\|thermal" > error_logs.txt 2>&1

# Method 3: Kernel ring buffer (no root needed)
dmesg | tail -n 500 > kernel_ring_buffer.txt 2>&1

cat error_logs.txt
cat kernel_ring_buffer.txt
