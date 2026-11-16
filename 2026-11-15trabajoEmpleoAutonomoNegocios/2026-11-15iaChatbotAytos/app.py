import re
import numpy as np
import torch
import gradio as gr
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer, AutoModelForCausalLM

# ---------------------------------------------------------------
# CONFIGURACION
# ---------------------------------------------------------------
LLM_ID = "BSC-LT/salamandra-2b-instruct"
EMB_ID = "intfloat/multilingual-e5-small"
USA_GPU = torch.cuda.is_available()
# En CPU gratuita: bfloat16 ocupa ~4,5 GB. Si va demasiado lento,
# prueba torch.float32 (ocupa ~9 GB, cabe en los 16 GB del Space).
DTYPE = torch.float16 if USA_GPU else torch.bfloat16
MAX_NUEVOS_TOKENS = 200
TAM_TROZO = 900      # caracteres por fragmento
SOLAPE = 150         # solape cuando un parrafo es muy largo

# ---------------------------------------------------------------
# CARGA DE MODELOS (una sola vez al arrancar)
# ---------------------------------------------------------------
dispositivo = "cuda" if USA_GPU else "cpu"
print("Cargando modelo de embeddings...")
embedder = SentenceTransformer(EMB_ID, device=dispositivo)
print("Cargando Salamandra-2B...")
tok = AutoTokenizer.from_pretrained(LLM_ID)
llm = AutoModelForCausalLM.from_pretrained(LLM_ID, dtype=DTYPE)
llm.to(dispositivo)
llm.eval()
print("Modelos listos.")

ESTADO = {"trozos": [], "emb": None}


# ---------------------------------------------------------------
# FUNCIONES RAG
# ---------------------------------------------------------------
def leer_texto(ruta):
    with open(ruta, "rb") as f:
        datos = f.read()
    for enc in ("utf-8", "latin-1"):
        try:
            return datos.decode(enc)
        except UnicodeDecodeError:
            pass
    return datos.decode("utf-8", errors="ignore")


def trocear(texto, max_car=TAM_TROZO, solape=SOLAPE):
    parrafos = [p.strip() for p in re.split(r"\n+", texto) if p.strip()]
    trozos, actual = [], ""
    for p in parrafos:
        if len(p) > max_car:
            if actual:
                trozos.append(actual)
                actual = ""
            i = 0
            while i < len(p):
                trozos.append(p[i:i + max_car])
                i += max_car - solape
            continue
        if actual and len(actual) + len(p) + 1 > max_car:
            trozos.append(actual)
            actual = p
        else:
            actual = (actual + "\n" + p) if actual else p
    if actual:
        trozos.append(actual)
    return trozos


def crear_indice(texto):
    trozos = trocear(texto)
    emb = embedder.encode(
        ["passage: " + t for t in trozos],
        normalize_embeddings=True,
        batch_size=16,
    )
    return trozos, emb


def buscar(pregunta, k):
    q = embedder.encode(["query: " + pregunta], normalize_embeddings=True)[0]
    puntos = ESTADO["emb"] @ q
    idx = sorted(np.argsort(-puntos)[:k].tolist())  # orden original del documento
    return [ESTADO["trozos"][i] for i in idx]


def generar(pregunta, fragmentos):
    contexto = "\n\n---\n\n".join(fragmentos)
    instruccion = (
        "Responde a la pregunta usando SOLO el texto del documento oficial. "
        "Si la respuesta no aparece en el texto, responde exactamente: "
        "No consta en el documento. "
        "Responde en espanol, de forma breve, indicando nombres completos y cargos."
    )
    prompt = f"{instruccion}\n\nDOCUMENTO:\n{contexto}\n\nPREGUNTA: {pregunta}"
    mensajes = [{"role": "user", "content": prompt}]
    entrada = tok.apply_chat_template(
        mensajes, add_generation_prompt=True, return_tensors="pt", return_dict=True
    )
    entrada = {k: v.to(dispositivo) for k, v in entrada.items()}
    with torch.no_grad():
        salida = llm.generate(
            **entrada, max_new_tokens=MAX_NUEVOS_TOKENS, do_sample=False
        )
    nuevos = salida[0][entrada["input_ids"].shape[1]:]
    return tok.decode(nuevos, skip_special_tokens=True).strip()


# ---------------------------------------------------------------
# INTERFAZ
# ---------------------------------------------------------------
def cargar_documento(archivo):
    if archivo is None:
        return "Primero sube un archivo .txt"
    ruta = archivo if isinstance(archivo, str) else archivo.name
    texto = leer_texto(ruta)
    if not texto.strip():
        return "El archivo esta vacio."
    ESTADO["trozos"], ESTADO["emb"] = crear_indice(texto)
    return (
        f"Documento cargado: {len(texto)} caracteres, "
        f"{len(ESTADO['trozos'])} fragmentos. Ya puedes preguntar."
    )


def preguntar(pregunta, k):
    if ESTADO["emb"] is None:
        return "Primero carga un documento.", ""
    if not pregunta.strip():
        return "Escribe una pregunta.", ""
    fragmentos = buscar(pregunta, int(k))
    respuesta = generar(pregunta, fragmentos)
    return respuesta, "\n\n=====\n\n".join(fragmentos)


with gr.Blocks(title="RAG con Salamandra-2B") as demo:
    gr.Markdown("# RAG con Salamandra-2B\nSube un documento oficial en .txt y pregunta.")
    archivo = gr.File(label="Documento .txt", file_types=[".txt"], type="filepath")
    boton_cargar = gr.Button("1. Cargar documento")
    estado = gr.Textbox(label="Estado", interactive=False)
    pregunta = gr.Textbox(label="Pregunta", placeholder="Por ejemplo: A quien nombran?")
    k = gr.Slider(2, 8, value=4, step=1, label="Fragmentos a consultar")
    boton_preguntar = gr.Button("2. Preguntar")
    respuesta = gr.Textbox(label="Respuesta", lines=6)
    fuentes = gr.Textbox(label="Fragmentos usados (para comprobar)", lines=10)

    boton_cargar.click(cargar_documento, inputs=archivo, outputs=estado)
    boton_preguntar.click(preguntar, inputs=[pregunta, k], outputs=[respuesta, fuentes])

demo.launch()
