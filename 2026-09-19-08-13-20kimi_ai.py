import json, urllib.request

API_KEY = "TU_KEY_MOONSHOT"
URL = "https://api.moonshot.ai/v1/chat/completions"
MODEL = "kimi-k2-turbo-preview"

messages = []
print("Kimi — Ctrl+C para salir\n")

while True:
    try:
        pregunta = input("Tú: ")
    except (EOFError, KeyboardInterrupt):
        break
    messages.append({"role": "user", "content": pregunta})
    data = json.dumps({"model": MODEL, "messages": messages}).encode()
    req = urllib.request.Request(URL, data=data, headers={
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    })
    with urllib.request.urlopen(req) as resp:
        respuesta = json.load(resp)["choices"][0]["message"]["content"]
    messages.append({"role": "assistant", "content": respuesta})
    print(f"\nDeepSeek: {respuesta}\n")
