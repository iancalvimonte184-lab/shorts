
import io
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

app = FastAPI(title="MiniShorts API")

# La API solo procesa archivos enviados por el usuario.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

MAX_BYTES = 300 * 1024 * 1024
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}


@app.get("/")
def home():
    return {
        "app": "MiniShorts",
        "status": "online",
        "endpoint": "/api/split"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/split")
async def split_video(
    video: UploadFile = File(...),
    seconds: int = Form(30),
):
    if seconds not in (15, 30, 45, 60, 90):
        raise HTTPException(
            status_code=400,
            detail="Elegí 15, 30, 45, 60 o 90 segundos."
        )

    ext = Path(video.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Formato no admitido. Subí MP4, MOV, MKV, WEBM o AVI."
        )

    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "original" + ext)
        total_bytes = 0

        try:
            with open(source, "wb") as out:
                while True:
                    chunk = await video.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > MAX_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="El archivo supera el límite de 300 MB."
                        )
                    out.write(chunk)
        finally:
            await video.close()

        if total_bytes == 0:
            raise HTTPException(
                status_code=400,
                detail="El archivo está vacío."
            )

        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                source,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if probe.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail="No se pudo leer el video."
            )

        try:
            duration = float(probe.stdout.strip())
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="No se pudo conocer la duración del video."
            )

        if duration <= 0 or duration > 3600:
            raise HTTPException(
                status_code=400,
                detail="El video debe durar entre 1 segundo y 60 minutos."
            )

        clips_dir = os.path.join(tmp, "clips")
        os.makedirs(clips_dir)

        count = 0
        start = 0.0

        while start < duration:
            output = os.path.join(
                clips_dir, f"short_{count + 1:03d}.mp4"
            )
            clip_length = min(seconds, duration - start)

            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-y",
                "-ss", str(start),
                "-i", source,
                "-t", str(clip_length),
                "-map", "0:v:0",
                "-map", "0:a?",
                "-vf",
                "scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "23",
                "-c:a", "aac",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-sn",
                output,
            ]

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300
            )

            if result.returncode != 0:
                raise HTTPException(
                    status_code=500,
                    detail="Falló el procesamiento de un clip."
                )

            count += 1
            start += seconds

        if count == 0:
            raise HTTPException(
                status_code=500,
                detail="No se pudieron generar clips."
            )

        memory_file = io.BytesIO()

        with zipfile.ZipFile(
            memory_file, "w", zipfile.ZIP_DEFLATED
        ) as archive:
            for name in sorted(os.listdir(clips_dir)):
                archive.write(
                    os.path.join(clips_dir, name),
                    arcname=name
                )

        memory_file.seek(0)

        return StreamingResponse(
            memory_file,
            media_type="application/zip",
            headers={
                "Content-Disposition":
                    'attachment; filename="minishorts.zip"'
            },
        )
