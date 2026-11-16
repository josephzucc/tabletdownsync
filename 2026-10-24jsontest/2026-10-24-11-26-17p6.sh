import json

data = {
    "items": [
        {"name": "apple", "count": 3},
        {"name": "banana", "count": 5}
    ]
}

print(json.dumps(data))
