import os, io, json, tempfile, logging
from itertools import chain

import fitz  # PyMuPDF
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package
from PIL import Image

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ───── CONFIG ────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

IMAGE_FOLDER = "images"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# ───── HELPER: Get Drive service ─────────────
def get_drive_service():
    sa_json = os.environ.get("SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise RuntimeError("Missing SERVICE_ACCOUNT_JSON env var")
    info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)

# ───── HELPER: Find image folder matching lecture ─────
def find_matching_folder_for_pdf(pdf_file_id):
    service = get_drive_service()
    meta = service.files().get(fileId=pdf_file_id, fields="name,parents").execute()
    pdf_name = meta["name"]
    parent_id = meta["parents"][0]
    folder_name = pdf_name[:-4] if pdf_name.lower().endswith(".pdf") else pdf_name

    query = f"'{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and name='{folder_name}' and trashed=false"
    resp = service.files().list(q=query, fields="files(id,name)").execute()
    matches = resp.get("files", [])
    return matches[0]["id"] if matches else None

# ───── HELPER: Download images ────────────────
def download_images_from_drive(folder_id, dest_folder):
    os.makedirs(dest_folder, exist_ok=True)
    service = get_drive_service()
    query = f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false"
    files = service.files().list(q=query, fields="files(id,name)").execute().get("files", [])
    downloaded = []

    for f in files:
        name = f["name"]
        path = os.path.join(dest_folder, name)
        request = service.files().get_media(fileId=f["id"])
        with io.FileIO(path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        try:
            img = Image.open(path)
            img.thumbnail((1024, 1024))
            img.save(path, format="JPEG", quality=70)
        except Exception:
            pass
        downloaded.append(path)
    return downloaded

# ───── ROUTE: Extract text from PDF ───────────
@app.route("/extract-text", methods=["POST"])
def extract_text():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    uploaded = request.files["file"]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        uploaded.save(tmp.name)
        doc = fitz.open(tmp.name)
    slides = [{"slide_number": i + 1, "text": page.get_text().strip()} for i, page in enumerate(doc) if page.get_text().strip()]
    doc.close()
    os.remove(tmp.name)
    return jsonify({"slides": slides})

# ───── ROUTE: Generate Anki Deck (.apkg) ──────
@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    data = request.get_json(force=True)
    raw_cards = data.get("cards") or data.get("Array") or []
    deck_name = data.get("deck_name", "Lecture Deck")
    lecture_file_id = data.get("lecture_file_drive_id")
    image_folder_id = data.get("image_folder_drive_id")

    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    if not image_folder_id and lecture_file_id:
        image_folder_id = find_matching_folder_for_pdf(lecture_file_id)

    tmp_dir = f"/tmp/{deck_name.replace(' ', '_')}"
    media_files = download_images_from_drive(image_folder_id, tmp_dir) if image_folder_id else []

    # Build deck
    cards = []
    for idx, c in enumerate(raw_cards, 1):
        slide_no = c.get("slide_number") or idx
        _img = c.get("image") or ""
        matched_path = None

        if "<img src='" in _img:
            fname = _img.split("<img src='")[1].split("'")[0]
            full_path = os.path.join(tmp_dir, fname)
            if os.path.exists(full_path):
                matched_path = full_path

        if not matched_path:
            suffix = f"-{str(slide_no).zfill(5)}.jpg"
            for f in os.listdir(tmp_dir):
                if f.endswith(suffix):
                    matched_path = os.path.join(tmp_dir, f)
                    break

        img_field = f"<img src='{os.path.basename(matched_path)}'>" if matched_path else ""
        if matched_path:
            media_files.append(matched_path)

        cards.append({
            "question": c.get("question", f"Card {idx}"),
            "answer": c.get("answer", ""),
            "explanation": f"{c.get('explanation', '')} (Slide {slide_no})",
            "image": img_field,
        })

    model = Model(
        1607392319,
        "Styled Lecture Model",
        fields=[{"name": "Question"}, {"name": "Answer"}, {"name": "Explanation"}, {"name": "Image"}],
        templates=[{
            "name": "Card 1",
            "qfmt": "<div class='question'>{{Question}}</div>",
            "afmt": """
<div class='question'>{{Question}}</div>
<hr>
<div class='answer'>{{Answer}}</div>
<div class='explanation'>{{Explanation}}</div>
<div class='image'>{{Image}}</div>"""
        }],
        css="""
.card { font-family:Arial; font-size:26px; text-align:center; background:#1e1e1e; color:#fff; }
.question { font-size:28px; margin-bottom:10px; }
.answer   { color:#4da6ff; font-weight:bold; margin:8px 0; }
.explanation { color:#ff66cc; font-style:italic; margin-top:10px; }
.image { color:#aaa; font-size:18px; margin-top:12px; }
.card img { display:block; margin:0 auto; transform:scale(.6); transform-origin:center; transition:transform .3s ease-in-out; cursor:pointer; }
.card img:hover { transform:scale(1); }
"""
    )

    deck = Deck(20504900110, deck_name)
    for c in cards:
        deck.add_note(Note(model=model, fields=[c["question"], c["answer"], c["explanation"], c["image"]]))

    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck, media_files=media_files).write_to_file(tmpf.name)
    return send_file(tmpf.name, as_attachment=True, download_name=f"{deck_name}.apkg")

@app.route("/", methods=["GET"])
def home():
    return "✅ PDF Slide Extractor Backend is Live", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
