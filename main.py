import sys
import os, io, json, tempfile, logging
from itertools import chain

import fitz                              # PyMuPDF
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image
from werkzeug.exceptions import RequestEntityTooLarge

# ── Configuration ─────────────────────────────────────────────────
app = Flask(__name__)

# allow up to 200 MB request bodies
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

# ── Logging setup ─────────────────────────────────────────────────
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.INFO)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

IMAGE_FOLDER = "images"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
SERVICE_ACCOUNT_FILE = "credentials/drive_service_account.json"
MEDIA_EXTS = (".png", ".jpg", ".jpeg")

# ── Helper: Drive service ─────────────────────────────────────────
def get_drive_service():
    app.logger.info("🔑 Obtaining Drive service credentials")
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    else:
        sa_json = os.environ.get("SERVICE_ACCOUNT_JSON")
        if not sa_json:
            raise RuntimeError(
                "Google Drive service account key not found: neither file nor SERVICE_ACCOUNT_JSON env var set."
            )
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)

# ── Request logging ─────────────────────────────────────────────────
@app.before_request
def log_request_info():
    size = request.content_length
    app.logger.info(f"➡️ Incoming {request.method} {request.path} with content length: {size} bytes")

# ── 413 Error handler ────────────────────────────────────────────────
@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    app.logger.error(f"❌ RequestEntityTooLarge: size {request.content_length} > MAX_CONTENT_LENGTH")
    return jsonify({"error": "Request entity too large"}), 413

# ── Helper: find image folder ────────────────────────────────────────
def find_matching_folder_for_pdf(pdf_file_id: str):
    app.logger.info(f"🔍 Finding image folder for PDF file ID {pdf_file_id}")
    service = get_drive_service()
    meta = service.files().get(fileId=pdf_file_id, fields="name,parents").execute()
    name = meta.get("name", "").rstrip()
    folder_name = name[:-4] if name.lower().endswith(".pdf") else name
    parent_id = meta.get("parents", [None])[0]
    if not parent_id:
        return None
    qry = (
        f"'{parent_id}' in parents and "
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{folder_name}' and trashed=false"
    )
    resp = service.files().list(q=qry, fields="files(id)").execute().get("files", [])
    folder_id = resp[0]["id"] if resp else None
    app.logger.info(f"📁 Auto-detected image folder ID: {folder_id}")
    return folder_id

# ── Helper: download images ─────────────────────────────────────────
def download_images_from_drive(folder_id: str, dest_folder: str):
    app.logger.info(f"⬇️ Downloading images from Drive folder {folder_id}")
    service = get_drive_service()
    os.makedirs(dest_folder, exist_ok=True)
    qry = f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false"
    files = service.files().list(q=qry, fields="files(id,name)").execute().get("files", [])
    downloaded = []
    for f in files:
        name = f.get("name", "")
        if not name.lower().endswith(MEDIA_EXTS):
            continue
        path = os.path.join(dest_folder, name)
        with io.FileIO(path, "wb") as fh:
            dl = MediaIoBaseDownload(fh, service.files().get_media(fileId=f["id"]))
            done = False
            while not done:
                _, done = dl.next_chunk()
        try:
            img = Image.open(path)
            img.thumbnail((1024, 1024))
            img.save(path, format="JPEG", quality=70)
        except Exception as e:
            app.logger.warning(f"Could not optimize image {name}: {e}")
        downloaded.append(path)
    app.logger.info(f"✅ Downloaded {len(downloaded)} images")
    return downloaded

# ── extract-text endpoint ───────────────────────────────────────────
@app.route("/extract-text", methods=["POST"])
def extract_text():
    try:
        app.logger.info("▶️ /extract-text called")
        if "file" not in request.files:
            app.logger.error("❌ No file uploaded to /extract-text")
            return jsonify({"error": "No file uploaded"}), 400
        uploaded = request.files["file"]
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            uploaded.save(tmp.name)
            doc = fitz.open(tmp.name)
        slides = [
            {"slide_number": i, "text": page.get_text().strip()}
            for i, page in enumerate(doc, start=1) if page.get_text().strip()
        ]
        doc.close()
        os.remove(tmp.name)
        app.logger.info(f"📃 Extracted text from {len(slides)} slides")
        return jsonify({"slides": slides})
    except Exception:
        app.logger.exception("❌ Unexpected error in /extract-text")
        return jsonify({"error": "Internal server error"}), 500

# ── generate-apkg endpoint ──────────────────────────────────────────
@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    try:
        app.logger.info("▶️ /generate-apkg called")
        app.logger.info(f"   • request size: {request.content_length} bytes")
        data = request.get_json(silent=True)
        app.logger.info(f"   • payload type: {type(data)}")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except:
                app.logger.error("❌ Bad JSON in /generate-apkg")
                return jsonify({"error": "Bad JSON"}], 400)
        if isinstance(data, list):
            raw_cards = data
            deck_name = "Lecture Deck"
            lecture_file_id = None
        else:
            raw_cards = data.get("cards", [])
            deck_name = data.get("deck_name", "Lecture Deck")
            lecture_file_id = data.get("lecture_file_drive_id")
        if not raw_cards:
            app.logger.error("❌ Missing cards in /generate-apkg payload")
            return jsonify({"error": "Missing cards"}], 400)
        app.logger.info(f"   • parsing {len(raw_cards)} cards")

        tmp_img_folder = None
        media_files = []
        if lecture_file_id:
            try:
                folder_id = find_matching_folder_for_pdf(lecture_file_id)
                if folder_id:
                    tmp_img_folder = f"/tmp/{deck_name.replace(' ','_')}"
                    media_files = download_images_from_drive(folder_id, tmp_img_folder)
            except Exception as e:
                app.logger.warning(f"Could not auto‑download images: {e}")

        processed = []
        for idx, c in enumerate(raw_cards, start=1):
            q = c.get("question", f"Card {idx}")
            a = c.get("answer", "")
            e = c.get("explanation", "")
            sn = c.get("slide_number") or idx
            nums = sn if isinstance(sn, list) else [sn]
            img_tags = []
            for num in nums:
                suffix = f"-{str(num).zfill(5)}.jpg"
                for folder in (tmp_img_folder, IMAGE_FOLDER):
                    if folder and os.path.isdir(folder):
                        for name in os.listdir(folder):
                            if name.lower().endswith(suffix):
                                media_files.append(os.path.join(folder, name))
                                img_tags.append(f"<img src='{name}'>")
                                break
            processed.append({
                "question": q,
                "answer": a,
                "explanation": e,
                "image": "".join(img_tags),
                "slide_number": nums
            })
        app.logger.info(f"   • built {len(processed)} flashcards")

        model = Model(
            1607392319,
            "Lecture Model",
            fields=[
                {"name":"Question"},
                {"name":"Answer"},
                {"name":"Explanation"},
                {"name":"Image"},
                {"name":"Slide Number"}
            ],
            templates=[{
                "name":"Card 1",
                "qfmt":"<div class='question'>{{Question}}</div>",
                "afmt":
"""
<div class='question'>{{Question}}</div>
<hr>
<div class='answer'>{{Answer}}</div>
<div class='explanation'>{{Explanation}}</div>
<div class='image'>{{Image}}</div>
<div class='slide-number'>Slide {{Slide Number}}</div>
"""
            }],
            css="""
.card{font-family:Arial;font-size:26px;text-align:center;background:#1e1e1e;color:#fff;}
.question{font-size:28px;margin-bottom:10px;}
.answer{color:#4da6ff;font-weight:bold;margin:8px 0;}
.explanation{color:#ff66cc;font-style:italic;margin-top:10px;}
.image{margin-top:12px;}
.slide-number{margin-top:8px;color:#aaa;font-size:18px;}
.card img{display:block;margin:0 auto;transform:scale(.6);cursor:pointer;}
.card img:hover{transform:scale(1);}
"""
        )

        deck = Deck(20504900110, deck_name)
        for note in processed:
            n = Note(
                model=model,
                fields=[
                    note["question"],
                    note["answer"],
                    note["explanation"],
                    note["image"],
                    ",".join(str(x) for x in note["slide_number"])
                ]
            )
            deck.add_note(n)

        tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
        Package(deck, media_files=media_files).write_to_file(tmpf.name)
        app.logger.info("✔️ Done; sending .apkg")
        return send_file(tmpf.name, as_attachment=True, download_name=f"{deck_name}.apkg")
    except Exception:
        app.logger.exception("❌ Unexpected error in /generate-apkg")
        return jsonify({"error": "Internal server error"}], 500)

@app.route("/", methods=["GET"])
def home():
    return "Server is running ✅", 200

if __name__ == '__main__':
    app.logger.info("🏁 Starting Flask server on 0.0.0.0:10000")
    app.run(host='0.0.0.0', port=10000)
