#!/usr/bin/env python3
# traducir.py - Translate German / Swiss German .txt to English with a small local LLM (llama.cpp) in Termux.
# Usage:  python traducir.py post.txt              -> creates post_en.txt
#         python traducir.py post.txt out.txt
#         python traducir.py post.txt -m ~/modelos/otro.gguf
# Only Python standard library needed.

import argparse, atexit, json, os, re, subprocess, sys, time
import urllib.request, urllib.error

PORT = 8081
BASE = f"http://127.0.0.1:{PORT}"
DEFAULT_MODEL = os.path.expanduser("~/modelos/qwen2.5-1.5b-instruct-q4_k_m.gguf")
MAX_CHARS = 1000  # max characters per chunk sent to the model

SYSTEM = (
    "You are a professional translator. Translate the user's text from German or Swiss German "
    "(any Swiss dialect, e.g. Zuerituetsch, Baerndeutsch, Baseldytsch) into natural, fluent English. "
    "Rules: output ONLY the English translation, no comments, no notes, no quotes around it. "
    "Keep the same paragraph and line structure. Keep hashtags, @mentions, URLs, numbers, emojis "
    "and proper names unchanged. If part of the text is already English, keep it as is."
)


def health():
    try:
        with urllib.request.urlopen(BASE + "/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start_server(model, threads, ctx):
    if health():
        return None
    if not os.path.isfile(model):
        sys.exit(f"Model not found: {model}\nSee instrucciones_traducir.txt to download it.")
    log = open(os.path.expanduser("~/llama_server.log"), "w")
    cmd = ["llama-server", "-m", model, "--port", str(PORT), "--host", "127.0.0.1",
           "-c", str(ctx), "-t", str(threads)]
    print("Starting local model (first load can take 20-60 s)...", file=sys.stderr)
    try:
        p = subprocess.Popen(cmd, stdout=log, stderr=log)
    except FileNotFoundError:
        sys.exit("llama-server not found. Run: pkg install llama-cpp")
    for _ in range(240):
        if p.poll() is not None:
            sys.exit("llama-server exited. Check ~/llama_server.log")
        if health():
            return p
        time.sleep(1)
    p.terminate()
    sys.exit("Timeout waiting for the model. Check ~/llama_server.log")


def split_chunks(text):
    paras = re.split(r"\n\s*\n", text.strip())
    chunks, cur = [], ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(para) > MAX_CHARS:  # split very long paragraph by sentences
            parts = re.split(r"(?<=[.!?])\s+", para)
        else:
            parts = [para]
        for part in parts:
            while len(part) > MAX_CHARS:  # hard split
                chunks_add = part[:MAX_CHARS]
                if cur:
                    chunks.append(cur); cur = ""
                chunks.append(chunks_add)
                part = part[MAX_CHARS:]
            if cur and len(cur) + len(part) + 2 > MAX_CHARS:
                chunks.append(cur); cur = part
            else:
                cur = (cur + "\n\n" + part) if cur else part
    if cur:
        chunks.append(cur)
    return chunks


def translate(chunk):
    body = {
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": chunk}],
        "temperature": 0.1,
        "max_tokens": 1500,
    }
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entrada")
    ap.add_argument("salida", nargs="?")
    ap.add_argument("-m", "--modelo", default=os.environ.get("TRAD_MODEL", DEFAULT_MODEL))
    ap.add_argument("-t", "--threads", type=int, default=4)
    ap.add_argument("-c", "--ctx", type=int, default=4096)
    ap.add_argument("--keep", action="store_true", help="leave the model server running afterwards")
    a = ap.parse_args()

    with open(a.entrada, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if not text.strip():
        sys.exit("Input file is empty.")
    out = a.salida or re.sub(r"\.txt$", "", a.entrada) + "_en.txt"

    proc = start_server(os.path.expanduser(a.modelo), a.threads, a.ctx)
    if proc and not a.keep:
        atexit.register(proc.terminate)

    chunks = split_chunks(text)
    result = []
    t0 = time.time()
    for i, c in enumerate(chunks, 1):
        print(f"Translating {i}/{len(chunks)}...", file=sys.stderr)
        result.append(translate(c))
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n\n".join(result) + "\n")
    print(f"Done in {int(time.time()-t0)} s -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
