import json
import sys

result = {"name": "Ada", "count": 3}
print(json.dumps(result))

print("Finished processing", file=sys.stderr)
