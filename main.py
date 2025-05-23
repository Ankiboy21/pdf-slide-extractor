import os, io, json, tempfile, logging
from itertools import chain

import fitz                              # PyMuPDF
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image

# ── Configuration ─────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

IMAGE_FOLDER = "images"  # local fallback for slide‐matched images
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
SERVICE_ACCOUNT_FILE = "credentials/drive_service_account.json"
MEDIA_EXTS = (".png", ".jpg", ".jpeg")

# ────────────────────────────────────────────────────────────
# Helper: get an authorized Drive service
# ────────────────────────────────────────────────────────────
def get_drive_service():
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

# ────────────────────────────────────────────────────────────
# Helper: find matching image folder for a PDF
# ────────────────────────────────────────────────────────────
def find_matching_folder_for_pdf(pdf_file_id: str):
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
    return resp[0]["id"] if resp else None

# ────────────────────────────────────────────────────────────
# Helper: download all images from Drive folder
# ────────────────────────────────────────────────────────────
def download_images_from_drive(folder_id: str, dest_folder: str):
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
        req = service.files().get_media(fileId=f["id"])
        with io.FileIO(path, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req)
            done = False
            while not done:
                _, done = dl.next_chunk()
        # Optimize
        try:
            img = Image.open(path)
            img.thumbnail((1024, 1024))
            img.save(path, format="JPEG", quality=70)
        except Exception:
            pass
        downloaded.append(path)
    return downloaded

# ── extract-text endpoint ─────────────────────────────────
@app.route("/extract-text", methods=["POST"])
def extract_text():
    if "file" not in request.files:
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
    return jsonify({"slides": slides})

# ── generate-apkg endpoint ─────────────────────────────────
@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    data = request.get_json(silent=True)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except:
            return jsonify({"error": "Bad JSON"}), 400
    # Determine incoming cards
    if isinstance(data, list):
        raw_cards = data
        deck_name = "Lecture Deck"
        lecture_file_id = None
    else:
        raw_cards = data.get("cards", [])
        deck_name = data.get("deck_name", "Lecture Deck")
        lecture_file_id = data.get("lecture_file_drive_id")
    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    # Auto‑detect image folder
    tmp_img_folder = None
    media_files = []
    if lecture_file_id:
        try:
            folder_id = find_matching_folder_for_pdf(lecture_file_id)
            if folder_id:
                tmp_img_folder = f"/tmp/{deck_name.replace(' ','_')}"
                media_files = download_images_from_drive(folder_id, tmp_img_folder)
        except Exception:
            logging.warning("Could not auto‑download images")

    # Prepare cards with image tags and slide_number field
    processed = []
    for idx, c in enumerate(raw_cards, start=1):
        q = c.get("question","Card {idx}")
        a = c.get("answer","")
        e = c.get("explanation","")
        sn = c.get("slide_number") or idx
        nums = sn if isinstance(sn, list) else [sn]
        # Match images by slide numbers
        img_tags = []
        for num in nums:
            suffix = f"-{str(num).zfill(5)}.jpg"
            for folder in (tmp_img_folder, IMAGE_FOLDER):
                if folder and os.path.isdir(folder):
                    for name in os.listdir(folder):
                        if name.lower().endswith(suffix):
                            path = os.path.join(folder, name)
                            media_files.append(path)
                            img_tags.append(f"<img src='{name}'>")
                            break
        img_field = "".join(img_tags)
        processed.append({
            "question":    q,
            "answer":      a,
            "explanation": e,
            "image":       img_field,
            "slide_number": nums
        })

    # Build Anki model with Slide Number field
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

    # Create deck and add notes
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

    # Write and return .apkg
    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck, media_files=media_files).write_to_file(tmpf.name)
    return send_file(tmpf.name, as_attachment=True, download_name=f"{deck_name}.apkg")

@app.route("/", methods=["GET"])
def home():
    return "Server is running ✅", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
