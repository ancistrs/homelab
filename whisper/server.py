from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import JSONResponse
from faster_whisper import WhisperModel
import tempfile, os, logging, traceback

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("whisper")

app = FastAPI()
model = WhisperModel("large-v3-turbo", device="auto", compute_type="int8")

@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": model is not None}

@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), language: str | None = Form(None)):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not ready")
    tmp_path = None
    try:
        suffix = os.path.splitext(file.filename or "")[-1] or ".tmp"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk: break
                tmp.write(chunk)
            tmp_path = tmp.name
        log.info(f"Transcribing: {file.filename} (lang={language})")
        segments, info = model.transcribe(tmp_path, language=language)
        text = " ".join(s.text.strip() for s in segments)
        return JSONResponse({"text": text})
    except Exception as e:
        log.error("Transcription failed: %s\n%s", e, traceback.format_exc())
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass
