import os
import uuid
import shutil
import tempfile
from pathlib import Path

import fitz  # PyMuPDF
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pdf2docx import Converter

app = FastAPI(title="PDF to Word Converter API")

# Allow the mobile app (and browser testing) to call this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Configuration ----
MAX_FILE_SIZE_MB = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
TEMP_DIR = Path(tempfile.gettempdir()) / "pdf2word"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def _is_scanned_pdf(pdf_path: str) -> bool:
    """Return True if the PDF has little/no extractable text (likely scanned)."""
    try:
        doc = fitz.open(pdf_path)
        total_text = ""
        for page in doc:
            total_text += page.get_text()
        doc.close()
        # Heuristic: fewer than 20 chars per page on average => probably scanned
        if len(doc) == 0:
            return True
        return len(total_text.strip()) < (20 * len(doc))
    except Exception:
        return False


@app.get("/")
def health():
    return {"status": "ok", "service": "pdf-to-word"}


@app.post("/convert")
async def convert(file: UploadFile = File(...)):
    # ---- 1. Validate type ----
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are allowed.")

    # ---- 2. Save upload to a temp file while enforcing size limit ----
    job_id = uuid.uuid4().hex
    pdf_path = TEMP_DIR / f"{job_id}.pdf"
    docx_path = TEMP_DIR / f"{job_id}.docx"

    size = 0
    try:
        with open(pdf_path, "wb") as out:
            while chunk := await file.read(1024 * 1024):  # 1 MB chunks
                size += len(chunk)
                if size > MAX_FILE_SIZE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File too large. Max {MAX_FILE_SIZE_MB} MB.",
                    )
                out.write(chunk)
    except HTTPException:
        pdf_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

    # ---- 3. Reject scanned PDFs early ----
    if _is_scanned_pdf(str(pdf_path)):
        pdf_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422,
            detail="This PDF looks like a scanned image and contains no "
                   "selectable text. OCR is not supported yet.",
        )

    # ---- 4. Convert ----
    try:
        cv = Converter(str(pdf_path))
        cv.convert(str(docx_path))
        cv.close()
    except Exception as e:
        pdf_path.unlink(missing_ok=True)
        docx_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Conversion failed: {e}")

    # ---- 5. Clean up the source PDF, return DOCX ----
    pdf_path.unlink(missing_ok=True)

    if not docx_path.exists() or docx_path.stat().st_size == 0:
        docx_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Conversion produced no output.")

    return FileResponse(
        path=str(docx_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="converted.docx",
        background=_cleanup_task(docx_path),
    )


def _cleanup_task(path: Path):
    """Delete the temp DOCX after the response is sent."""
    from starlette.background import BackgroundTask

    def _delete():
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    return BackgroundTask(_delete)


# Optional: run directly with `python main.py`
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)