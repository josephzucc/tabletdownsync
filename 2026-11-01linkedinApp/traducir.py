#!/usr/bin/env python3
# traducir.py v2 - Translate German / Swiss German .txt to English with a small local LLM (llama.cpp) in Termux.
# Usage:  python traducir.py post.txt                      -> creates post_en.txt
#         python traducir.py post.txt out.txt
#         python traducir.py post.txt --hasta '^_{10,}'     -> stops at the first line matching the regex
#                                                              (useful for newsletters that repeat the text in French)
#         python traducir.py post.txt -m ~/modelos/otro.gguf
# Only Python standard library needed.
#
# v2 changes (fix for "TimeoutError: timed out"):
#   - the answer is now STREAMED, so there is no 600 s wall-clock limit; it only fails if the model sends
#     nothing for 300 s in a row
#   - URLs are replaced by short placeholders before translating and restored afterwards (URLs eat many tokens
#     and make small models loop)
#   - smaller chunks and a max_tokens limit proportional to the chunk size (no endless repetition)
#   - one automatic retry per chunk; if it fails twice the original chunk is kept and marked
#   - the output file is saved after every chunk, plus a log file YYYY-MM-DD-HH-MM-SS_traducir_log.txt

import argparse, atexit, glob, json, os, re, subprocess, sys, time
import urllib.request, urllib.error

PORT = 8081
BASE = f"http://127.0.0.1:{PORT}"
MODELS_DIR = os.path.expanduser("~/modelos")
MAX_CHARS = 700        # max characters per chunk sent to the model
IDLE_TIMEOUT = 300     # seconds without receiving anything from the model

SYSTEM = (
    "You are a professional translator. Translate the user's text from German or Swiss German "
    "(any Swiss dialect) into natural, fluent English. "
    "Rules: output ONLY the English translation, no comments, no notes, no quotes around it. "
    "Keep the same paragraph and line structure. Keep placeholders like [[1]], hashtags, @mentions, "
    "numbers, emojis and proper names unchanged. If part of the text is already English, keep it as is. "
    "Never repeat a sentence."
)

LOG = []


def log(msg):
    LOG.append(msg)
    print(msg, file=sys.stderr, flush=True)


def default_model():
    env = os.environ.get("TRAD_MODEL")
    if env:
        return os.path.expanduser(env)
    for pat in ("qwen2.5-3b*.gguf", "qwen2.5-1.5b*.gguf", "*.gguf"):
        found = sorted(glob.glob(os.path.join(MODELS_DIR, pat)))
        if found:
            return found[0]
    return os.path.join(MODELS_DIR, "qwen2.5-1.5b-instruct-q4_k_m.gguf")


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
    logf = open(os.path.expanduser("~/llama_server.log"), "w")
    cmd = ["llama-server", "-m", model, "--port", str(PORT), "--host", "127.0.0.1",
           "-c", str(ctx), "-t", str(threads)]
    log(f"Starting local model {os.path.basename(model)} (first load can take 20-60 s)...")
    try:
        p = subprocess.Popen(cmd, stdout=logf, stderr=logf)
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


# ---------- URL placeholders ----------
URL_RE = re.compile(r"https?://[^\s<>]+")


def mask_urls(text, urls):
    def rep(m):
        u = m.group(0)
        tail = ""
        while u and u[-1] in ".,;:)]\"'":
            tail = u[-1] + tail
            u = u[:-1]
        urls.append(u)
        return f"[[{len(urls)}]]" + tail
    return URL_RE.sub(rep, text)


def unmask_urls(text, urls, expected):
    found = set()

    def rep(m):
        n = int(m.group(1))
        if 1 <= n <= len(urls):
            found.add(n)
            return urls[n - 1]
        return m.group(0)
    out = re.sub(r"\[\[\s*(\d+)\s*\]\]", rep, text)
    missing = [urls[n - 1] for n in expected if n not in found]
    if missing:
        out += "\n" + "\n".join(missing)
    return out


# ---------- chunking ----------
def split_chunks(text):
    paras = re.split(r"\n\s*\n", text.strip())
    chunks, cur = [], ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        parts = re.split(r"(?<=[.!?])\s+", para) if len(para) > MAX_CHARS else [para]
        for part in parts:
            while len(part) > MAX_CHARS:
                if cur:
                    chunks.append(cur); cur = ""
                chunks.append(part[:MAX_CHARS])
                part = part[MAX_CHARS:]
            if cur and len(cur) + len(part) + 2 > MAX_CHARS:
                chunks.append(cur); cur = part
            else:
                cur = (cur + "\n\n" + part) if cur else part
    if cur:
        chunks.append(cur)
    return chunks


# ---------- translation (streaming) ----------
def translate(chunk):
    body = {
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": chunk}],
        "temperature": 0.1,
        "repeat_penalty": 1.05,
        "max_tokens": int(len(chunk) / 2) + 80,
        "stream": True,
    }
    req = urllib.request.Request(BASE + "/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    parts = []
    with urllib.request.urlopen(req, timeout=IDLE_TIMEOUT) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"].get("content")
            except Exception:
                continue
            if delta:
                parts.append(delta)
                print(".", end="", file=sys.stderr, flush=True)
    print("", file=sys.stderr)
    return "".join(parts).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("entrada")
    ap.add_argument("salida", nargs="?")
    ap.add_argument("-m", "--modelo", default=default_model())
    ap.add_argument("-t", "--threads", type=int, default=4)
    ap.add_argument("-c", "--ctx", type=int, default=4096)
    ap.add_argument("--hasta", help="regex: ignore the text from the first line that matches it")
    ap.add_argument("--keep", action="store_true", help="leave the model server running afterwards")
    a = ap.parse_args()

    with open(a.entrada, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if a.hasta:
        rx = re.compile(a.hasta)
        kept = []
        for ln in text.splitlines():
            if rx.search(ln):
                break
            kept.append(ln)
        text = "\n".join(kept)
    if not text.strip():
        sys.exit("Input file is empty.")
    out = a.salida or re.sub(r"\.txt$", "", a.entrada) + "_en.txt"

    urls = []
    masked = mask_urls(text, urls)
    chunks = split_chunks(masked)

    proc = start_server(os.path.expanduser(a.modelo), a.threads, a.ctx)
    if proc and not a.keep:
        atexit.register(proc.terminate)

    result, failed = [], []
    t0 = time.time()
    try:
        for i, c in enumerate(chunks, 1):
            expected = [int(n) for n in re.findall(r"\[\[(\d+)\]\]", c)]
            log(f"Translating {i}/{len(chunks)} ({len(c)} chars)...")
            tr = None
            for attempt in (1, 2):
                try:
                    tr = translate(c)
                    if tr:
                        break
                except Exception as e:
                    log(f"  attempt {attempt} failed: {type(e).__name__}: {e}")
            if tr:
                result.append(unmask_urls(tr, urls, expected))
            else:
                failed.append(i)
                result.append("[NOT TRANSLATED]\n" + unmask_urls(c, urls, expected))
            with open(out, "w", encoding="utf-8") as f:   # saved after every chunk
                f.write("\n\n".join(result) + "\n")
    finally:
        log(f"Finished {len(result)}/{len(chunks)} chunks in {int(time.time()-t0)} s -> {out}")
        if failed:
            log("Chunks not translated: " + ", ".join(map(str, failed)))
        stamp = time.strftime("%Y-%m-%d-%H-%M-%S")
        logname = os.path.join(os.path.dirname(os.path.abspath(out)), f"{stamp}_traducir_log.txt")
        with open(logname, "w", encoding="utf-8") as f:
            f.write("\n".join(LOG) + "\n")


if __name__ == "__main__":
    main()
