"""Simple web interface for fire and smoke detection."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home() -> HTMLResponse:
    return HTMLResponse(
        """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fire Detection</title>
<style>
* { box-sizing: border-box; }
body {
    margin: 0;
    font-family: "Segoe UI", Arial, sans-serif;
    color: #202938;
    background: #f4f6fa;
}
header {
    background: #17243a;
    color: white;
    padding: 28px 20px;
}
header, main {
    padding-left: max(20px, calc((100% - 850px) / 2));
    padding-right: max(20px, calc((100% - 850px) / 2));
    }
header h1 { margin: 0 0 8px; }
header p { margin: 0; color: #d1d9e6; }
main { padding-top: 28px; padding-bottom: 40px; }
.card {
    background: white;
    border: 1px solid #e2e7ef;
    border-radius: 14px;
    padding: 24px;
    margin-bottom: 20px;
}
h2 { margin-top: 0; font-size: 20px; }
.hint { color: #64748b; line-height: 1.6; font-size: 14px; }
.upload {
    display: block;
    padding: 24px;
    text-align: center;
    border: 2px dashed #cbd5e1;
    border-radius: 12px;
    cursor: pointer;
}
.upload input { display: block; margin: 14px auto 0; max-width: 100%; }
button {
    width: 100%;
    margin-top: 16px;
    padding: 13px;
    border: 0;
    border-radius: 9px;
    background: #285bb5;
    color: white;
    font-size: 15px;
    font-weight: 600;
    cursor: pointer;
}
button:disabled { opacity: .6; cursor: wait; }
#preview {
    display: none;
    width: 100%;
    max-height: 400px;
    object-fit: contain;
    margin-top: 18px;
}
#status { margin-top: 14px; color: #64748b; font-size: 14px; }
#results { display: none; }
.stat {
    background: #f1f5f9;
    border-radius: 10px;
    padding: 14px;
    margin-bottom: 12px;
}
.stat span { display: block; color: #64748b; font-size: 13px; margin-bottom: 6px; }
.detection { border: 1px solid #e2e7ef; border-radius: 10px; padding: 16px; margin-top: 12px; }
.pill { color: #a13c12; background: #fff0e8; border-radius: 20px; padding: 5px 10px; }
.notice { padding: 15px; border-radius: 10px; line-height: 1.6; }
.success { background: #ecfdf3; color: #166534; }
.error { background: #fef2f2; color: #b91c1c; }
.coords { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; }
.coord { background: #f8fafc; border-radius: 8px; padding: 10px; }
.coord span { display: block; color: #64748b; font-size: 12px; }
.coord strong { display: block; margin-top: 5px; }
footer { text-align: center; color: #94a3b8; font-size: 13px; padding: 12px; }
@media(max-width: 520px) { .coords { grid-template-columns: repeat(2, 1fr); } }
</style>
</head>
<body>
<header>
    <h1>Fire and Smoke Detection</h1>
    <p>Upload an image to analyze it with the machine learning model.</p>
</header>
<main>
    <section class="card">
        <h2>1. Upload an image</h2>
        <p class="hint">Supported formats: JPEG, PNG and WEBP.</p>
        <form id="form">
            <label class="upload">
                <strong>Choose an image file</strong>
                <input id="file" name="file" type="file"
                    accept="image/jpeg,image/png,image/webp" required>
            </label>
            <img id="preview" alt="Selected image preview">
            <button id="submit" type="submit">Analyze image</button>
            <div id="status" role="status">Select an image to get started.</div>
        </form>
    </section>
    <section class="card" id="results">
        <h2>2. Detection results</h2>
        <div id="summary"></div>
        <div id="details"></div>
    </section>
    <footer>Fire Detection · MLflow · FastAPI</footer>
</main>
<script>
const form = document.getElementById("form");
const fileInput = document.getElementById("file");
const preview = document.getElementById("preview");
const submit = document.getElementById("submit");
const statusBox = document.getElementById("status");
const results = document.getElementById("results");
let previewUrl = null;

function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
}

fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    results.style.display = "none";
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;

    if (!file) {
        preview.style.display = "none";
        statusBox.textContent = "Select an image to get started.";
        return;
    }

    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
        preview.style.display = "none";
        statusBox.textContent = "Please select a JPEG, PNG or WEBP image.";
        return;
    }

    previewUrl = URL.createObjectURL(file);
    preview.src = previewUrl;
    preview.style.display = "block";
    statusBox.textContent = "Image selected. Ready to analyze.";
});

function showResult(data) {
    const summary = document.getElementById("summary");
    const details = document.getElementById("details");
    summary.replaceChildren();
    details.replaceChildren();

    const count = el("div", "stat");
    count.append(el("span", "", "Objects detected"));
    count.append(el("strong", "", String(data.detections_count ?? 0)));
    summary.append(count);

    const filename = el("div", "stat");
    filename.append(el("span", "", "File"));
    const name = el("strong", "", data.filename || "Uploaded image");
    name.style.overflowWrap = "anywhere";
    filename.append(name);
    summary.append(filename);

    if (!data.detections || data.detections.length === 0) {
        details.append(el("div", "notice success",
            "No objects were detected in this image."));
    } else {
        data.detections.forEach((d, i) => {
            const card = el("article", "detection");
            const heading = el("h3", "", "Detection " + (i + 1) + " ");
            heading.append(el("span", "pill", d.class_name || "Unknown"));
            card.append(heading);

            const confidence = Number(d.confidence);
            card.append(el("p", "hint", "Confidence: " +
                (Number.isFinite(confidence)
                    ? (confidence * 100).toFixed(2) + "%" : "Unknown")));

            const coords = el("div", "coords");
            [["x1", "Left"], ["y1", "Top"],
             ["x2", "Right"], ["y2", "Bottom"]].forEach(([key, label]) => {
                const item = el("div", "coord");
                item.append(el("span", "", label));
                const value = Number(d[key]);
                item.append(el("strong", "",
                    Number.isFinite(value) ? value.toFixed(1) : "—"));
                coords.append(item);
            });
            card.append(coords);
            details.append(card);
        });
    }

    results.style.display = "block";
    results.scrollIntoView({ behavior: "smooth", block: "start" });
}

form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = fileInput.files[0];
    if (!file) {
        statusBox.textContent = "Please select an image first.";
        return;
    }

    submit.disabled = true;
    submit.textContent = "Analyzing...";
    statusBox.textContent = "The model is processing your image. Please wait.";
    results.style.display = "none";

    try {
        const body = new FormData();
        body.append("file", file);

        const response = await fetch("/process", {
            method: "POST",
            body
        });
        const data = await response.json();

        if (!response.ok) {
            throw new Error(typeof data.detail === "string"
                ? data.detail : "Image processing failed.");
        }

        showResult(data);
        statusBox.textContent = "Analysis completed successfully.";
    } catch (error) {
        results.style.display = "block";
        document.getElementById("summary").replaceChildren();
        document.getElementById("details").replaceChildren(
            el("div", "notice error", "Error: " +
                (error.message || "Could not connect to the server."))
        );
        statusBox.textContent = "Could not process the image.";
    } finally {
        submit.disabled = false;
        submit.textContent = "Analyze image";
    }
});
</script>
</body>
</html>
"""
    )
