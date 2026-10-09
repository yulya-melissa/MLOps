"""API endpoint for fire and smoke detection."""

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Annotated, Any

import pandas as pd
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

router = APIRouter()
log = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}


@router.post("/process")
async def process_image(
    request: Request,
    file: Annotated[UploadFile, File(...)],
) -> dict[str, Any]:
    """Detect fire and smoke in an uploaded image."""

    suffix = ALLOWED_CONTENT_TYPES.get(file.content_type or "")
    if suffix is None:
        raise HTTPException(
            status_code=415,
            detail="Unsupported image type. Use JPEG, PNG or WEBP.",
        )

    model = request.app.state.inference_model

    temp_path: str | None = None

    try:
        content = await file.read()

        if not content:
            raise HTTPException(status_code=400, detail="Empty image file.")

        with tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name

        model_input = pd.DataFrame({"image_path": [temp_path]})

        result = await run_in_threadpool(
            model.predict,
            model_input,
        )

        detections = result.to_dict(orient="records")

        return {
            "filename": file.filename,
            "detections_count": len(detections),
            "detections": detections,
        }

    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Image inference failed")
        raise HTTPException(
            status_code=500,
            detail="Image inference failed.",
        ) from exc
    finally:
        await file.close()
        if temp_path is not None:
            await asyncio.to_thread(Path(temp_path).unlink, missing_ok=True)
