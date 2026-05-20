import os
import json
import shutil
import time
import base64
import re
import traceback
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from langchain_huggingface import HuggingFaceEmbeddings
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_community.vectorstores import Chroma
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

# ── Configuración ──────────────────────────────────────────────────────────────
DOCS_DIR      = os.path.join(os.path.dirname(__file__), "docs")
CHROMA_DIR    = os.path.join(os.path.dirname(__file__), "chroma_db")
INDEXED_FILE  = os.path.join(os.path.dirname(__file__), "indexed_files.json")

LLM_MODEL = "gemini-flash-latest"

# Cargar múltiples API Keys y limpiar espacios/vacíos
raw_keys = os.getenv("GOOGLE_API_KEY", "").split(",")
API_KEYS = [k.strip() for k in raw_keys if k.strip()]
current_key_index = 0

if not API_KEYS:
    print("⚠️ ¡ATENCIÓN! No se detectó ninguna GOOGLE_API_KEY en el archivo .env")
    print("Asegúrate de que el archivo .env exista en backend-python/ y tenga el formato: GOOGLE_API_KEY=tu_llave")

os.makedirs(DOCS_DIR,   exist_ok=True)
os.makedirs(CHROMA_DIR, exist_ok=True)

def _load_indexed() -> set:
    if os.path.exists(INDEXED_FILE):
        with open(INDEXED_FILE) as f: return set(json.load(f))
    return set()

def _save_indexed(indexed: set):
    with open(INDEXED_FILE, "w") as f: json.dump(list(indexed), f)

print("🧠 Cargando modelo de lenguaje local (HuggingFace)...")
EMBEDDINGS = HuggingFaceEmbeddings(model_name="all-MiniLM-L6-v2")

def _get_vectorstore():
    return Chroma(persist_directory=CHROMA_DIR, embedding_function=EMBEDDINGS)

def ingest_new_pdfs():
    indexed = _load_indexed()
    all_pdfs = [f for f in os.listdir(DOCS_DIR) if f.lower().endswith(".pdf")]
    new_pdfs = [f for f in all_pdfs if f not in indexed]
    
    if not new_pdfs:
        print("✅ Documentos al día. No hay PDFs nuevos para indexar.")
        return

    print(f"📂 Se encontraron {len(new_pdfs)} archivos nuevos. Iniciando indexación...")
    
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    
    for i, f in enumerate(new_pdfs, 1):
        try:
            print(f"📖 [{i}/{len(new_pdfs)}] Procesando: {f}...")
            loader = PyPDFLoader(os.path.join(DOCS_DIR, f))
            pages = loader.load()
            chunks = splitter.split_documents(pages)
            
            # Intentar guardar en Chroma, si falla por dimensiones, limpiar y reintentar
            try:
                Chroma.from_documents(documents=chunks, embedding=EMBEDDINGS, persist_directory=CHROMA_DIR)
            except Exception as e:
                if "dimension" in str(e).lower():
                    print("⚠️ Error de dimensión detectado en ChromaDB. Limpiando base de datos corrupta...")
                    if os.path.exists(CHROMA_DIR): shutil.rmtree(CHROMA_DIR)
                    os.makedirs(CHROMA_DIR, exist_ok=True)
                    indexed = set() # Reiniciar índice
                    Chroma.from_documents(documents=chunks, embedding=EMBEDDINGS, persist_directory=CHROMA_DIR)
                else: raise e
                
            indexed.add(f)
            _save_indexed(indexed)
        except Exception as e:
            print(f"❌ Error al procesar {f}: {e}")
            continue
            
    print("✨ Indexación completada con éxito.")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ejecutar ingesta al arrancar
    ingest_new_pdfs()
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class MessageDict(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    image: Optional[str] = None
    graphType: Optional[str] = "auto"
    history: Optional[List[MessageDict]] = []

@app.post("/chat")
def chat(request: ChatRequest):
    global current_key_index
    if not API_KEYS:
        raise HTTPException(status_code=500, detail="No se encontró ninguna API Key válida en el .env")
    
    try:
        vectorstore = _get_vectorstore()
        docs = vectorstore.as_retriever(search_kwargs={"k": 8}).invoke(request.message)
        context = "\n\n".join(d.page_content for d in docs)

        graph_instruction = ""
        if request.graphType == "cartesianas":
            graph_instruction = "ESTÁ OBLIGADO A USAR [CHART] (3 gráficos: Posición, Velocidad, Aceleración). PROHIBIDO USAR [DIAGRAM]."
        elif request.graphType == "polares":
            graph_instruction = "ESTÁ OBLIGADO A USAR [DIAGRAM] con coordenadas POLARES (r, theta). PROHIBIDO USAR [CHART]."
        elif request.graphType == "intrinsecas":
            graph_instruction = "ESTÁ OBLIGADO A USAR [DIAGRAM] con coordenadas INTRÍNSECAS (tangencial, normal). PROHIBIDO USAR [CHART]."
        elif request.graphType == "none":
            graph_instruction = "EL USUARIO PIDIÓ NO INCLUIR GRÁFICOS. PROHIBIDO USAR [CHART] Y [DIAGRAM]."
        else:
            graph_instruction = "Elige automáticamente la mejor visualización según el tipo de problema."

        system_prompt = f"""Eres un profesor de física experto de nivel universitario. Tu misión es resolver problemas y explicar conceptos con rigor académico.

REGLA DE CONTEXTO:
Evalúa si los CONCEPTOS FÍSICOS y las FÓRMULAS necesarias para resolver la pregunta del usuario pueden deducirse del CONTEXTO BIBLIOGRÁFICO provisto.
- Si los conceptos base (ej. cinemática, dinámica, conservación) están en el material, aplica esos principios para resolver el ejercicio y responde de forma normal, aunque el problema exacto no esté en los textos.
- SOLAMENTE si la pregunta requiere ramas de la física (ej. electromagnetismo avanzado, cuántica) o fórmulas que son COMPLETAMENTE AJENAS al material subido, DEBES comenzar tu respuesta exactamente con esta advertencia (en su propia línea):
"⚠️ **Aviso: Respuesta fuera del material subido!** ⚠️"

INSTRUCCIÓN DE VISUALIZACIÓN REQUERIDA POR EL USUARIO:
{graph_instruction}

GUÍA DE VISUALIZACIÓN (ELEGIR SEGÚN EL PROBLEMA):

1. CINEMÁTICA LINEAL / GRÁFICOS TEMPORALES ([CHART]):
   - Uso: Problemas de MRU/MRUV para ver evolución de x, v, a en el TIEMPO.
   - REGLA: Genera 3 bloques [CHART] (Posición, Velocidad y Aceleración).
   - Formato: [CHART] {{ "title": "...", "xAxis": "Tiempo (s)", "yAxis": "...", "series": [ {{ "name": "...", "data": [ {{"x": 0, "y": 0}}, ... ] }} ] }} [/CHART]

2. POLARES / INTRÍNSECAS / VECTORES ([DIAGRAM]):
   - Uso: Movimiento circular, trayectorias espaciales, versores y fuerzas.
   - REGLA: Calcula x e y (x=r*cos(ang), y=r*sin(ang)). Los versores deben iniciar en la partícula.
   - Formato: [DIAGRAM] {{ "title": "...", "zoom": 1, "elements": [ 
       {{"type": "circle", "r": 2}}, 
       {{"type": "point", "x": 1.41, "y": 1.41, "label": "A", "color": "#f00"}},
       {{"type": "vector", "x": 1.41, "y": 1.41, "vx": -1, "vy": 1, "label": "v_A"}},
       {{"type": "versor", "x": 1.41, "y": 1.41, "vx": 0.7, "vy": 0.7, "label": "e_r"}}
     ] }} [/DIAGRAM]

REGLAS ACADÉMICAS:
- Resolución PASO A PASO detallada.
- USA LaTeX ($...$) para todas las fórmulas.

CONTEXTO BIBLIOGRÁFICO:
{context}
"""
        messages_to_send = [SystemMessage(content=system_prompt)]
        for msg in request.history:
            if msg.role == "user":
                messages_to_send.append(HumanMessage(content=msg.content))
            else:
                messages_to_send.append(AIMessage(content=msg.content))

        content = [{"type": "text", "text": request.message}]
        if request.image: content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{request.image}"}})
        messages_to_send.append(HumanMessage(content=content))

        last_error = ""
        for _ in range(len(API_KEYS)):
            try:
                key = API_KEYS[current_key_index].strip()
                llm = ChatGoogleGenerativeAI(model=LLM_MODEL, google_api_key=key, temperature=0.1)
                raw = llm.invoke(messages_to_send).content
                if isinstance(raw, list): raw = "".join([p.get("text", "") if isinstance(p, dict) else str(p) for p in raw])

                charts = []; diagrams = []; clean = raw
                for m in re.findall(r"\[CHART\](.*?)\[/CHART\]", raw, re.DOTALL):
                    try: charts.append(json.loads(m.strip())); clean = clean.replace(f"[CHART]{m}[/CHART]", "")
                    except: pass
                for m in re.findall(r"\[DIAGRAM\](.*?)\[/DIAGRAM\]", raw, re.DOTALL):
                    try: diagrams.append(json.loads(m.strip())); clean = clean.replace(f"[DIAGRAM]{m}[/DIAGRAM]", "")
                    except: pass

                sources = [{"page": d.metadata.get("page", "?"), "source": os.path.basename(d.metadata.get("source", "?"))} for d in docs]
                return {"response": clean.strip(), "sources": sources, "charts": charts, "diagrams": diagrams}

            except Exception as e:
                last_error = str(e)
                if "429" in last_error or "403" in last_error or "PERMISSION_DENIED" in last_error:
                    print(f"⚠️ Cambiando API Key...")
                    current_key_index = (current_key_index + 1) % len(API_KEYS)
                    continue
                else: raise e

        raise HTTPException(status_code=500, detail=f"Agotado: {last_error}")
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/documents")
def docs(): return {"indexed": list(_load_indexed())}
