import os, io, json, tempfile
from itertools import chain

import fitz                              # PyMuPDF
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package

# ── Google-Drive API ────────────────────────────────────────────────
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
SERVICE_ACCOUNT_FILE = "credentials/drive_service_account.json"  # ← path to your .json key

# ── App & constants ────────────────────────────────────────────────
app = Flask(__name__)
IMAGE_FOLDER = "images"                     # local fallback folder
MEDIA_EXTS   = (".png", ".jpg", ".jpeg")    # allowed image types


# ╔═════════════════════════════════════════════════════════════════╗
# ║  Helper: download every PNG/JPG from a given Drive folder       ║
# ╚═════════════════════════════════════════════════════════════════╝
def download_images_from_drive(folder_id: str, dest_folder: str):
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    service = build("drive", "v3", credentials=creds)
    os.makedirs(dest_folder, exist_ok=True)

    query = (
        f"'{folder_id}' in parents and mimeType contains 'image/' "
        "and trashed = false"
    )
    files = (
        service.files()
        .list(q=query, fields="files(id, name)")
        .execute()
        .get("files", [])
    )

    downloaded = []
    for file in files:
        name = file["name"]
        if not name.lower().endswith(MEDIA_EXTS):
            continue

        path = os.path.join(dest_folder, name)
        request = service.files().get_media(fileId=file["id"])
        with io.FileIO(path, "wb") as fh:
            dl = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = dl.next_chunk()
        downloaded.append(path)

    return downloaded


# ╔═════════════════════════════════════════════════════════════════╗
# ║  /extract-text  (unchanged)                                     ║
# ╚═════════════════════════════════════════════════════════════════╝
@app.route("/extract-text", methods=["POST"])
def extract_text():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    pdf = request.files["file"]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        pdf.save(tmp.name)
        doc = fitz.open(tmp.name)

    slides = [
        {"slide_number": i, "text": p.get_text().strip()}
        for i, p in enumerate(doc, start=1)
        if p.get_text().strip()
    ]

    doc.close()
    os.remove(tmp.name)
    return jsonify({"slides": slides})


# ╔═════════════════════════════════════════════════════════════════╗
# ║  /generate-apkg  (updated)                                      ║
# ╚═════════════════════════════════════════════════════════════════╝
@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    data = request.get_json(silent=True)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return jsonify({"error": "Bad JSON"}), 400

    if not isinstance(data, (list, dict)):
        return jsonify({"error": "Body must be list or object"}), 400

    if isinstance(data, list):
        raw_cards, deck_name, drive_folder_id = data, "Lecture Deck", None
    else:
        raw_cards = data.get("cards") or data.get("Array") or []
        deck_name = data.get("deck_name", "Lecture Deck")
        drive_folder_id = data.get("image_folder_drive_id")

    if raw_cards and isinstance(raw_cards[0], list):
        raw_cards = list(chain.from_iterable(raw_cards))
    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    # ── 1.  download Drive images (if provided) ────────────────────
    drive_dir   = ""
    media_files = []
    if drive_folder_id:
        drive_dir = f"/tmp/{deck_name.replace(' ', '_')}"
        media_files.extend(download_images_from_drive(drive_folder_id, drive_dir))

    # ── 2.  build cards & match images ─────────────────────────────
    cards = []
    for idx, c in enumerate(raw_cards, 1):
        slide_no = c.get("slide_number") or idx
        img_tag  = c.get("image") or c.get("Image") or ""

        # ----------------------------------------------------------------
        #  Extract filename OR build expected suffix "-00001.jpg"
        # ----------------------------------------------------------------
        if "<img src='" in img_tag:
            try:
                fname = img_tag.split("<img src='")[1].split("'")[0]
            except IndexError:
                fname = ""
        else:
            fname = ""

        matched = None
        #  ① direct filename match (if provided) – check Drive then local
        if fname:
            p_drive = os.path.join(drive_dir, fname) if drive_dir else ""
            p_local = os.path.join(IMAGE_FOLDER, fname)
            matched = p_drive if p_drive and os.path.exists(p_drive) else (
                      p_local if os.path.exists(p_local) else None)

        #  ② filename pattern match if nothing matched yet
        if not matched:
            suffix = f"-{str(slide_no).zfill(5)}.jpg"   # …-00003.jpg
            # search Drive folder
            if drive_dir and os.path.exists(drive_dir):
                for f in os.listdir(drive_dir):
                    if f.lower().endswith(suffix):
                        matched = os.path.join(drive_dir, f)
                        break
            # search local /images
            if not matched and os.path.exists(IMAGE_FOLDER):
                for f in os.listdir(IMAGE_FOLDER):
                    if f.lower().endswith(suffix):
                        matched = os.path.join(IMAGE_FOLDER, f)
                        break

        if matched:
            media_files.append(matched)

        cards.append({
            "question": c.get("question")    or c.get("Question") or f"Card {idx}",
            "answer":   c.get("answer")      or c.get("Answer")   or "",
            "explanation": c.get("explanation") or c.get("Explanation") or "",
            "image":    img_tag,
            "slide_number": slide_no
        })

    # ── 3.  build Anki deck ────────────────────────────────────────
    model = Model(
        1607392319,
        "Styled Lecture Model",
        fields=[
            {"name": "Question"},
            {"name": "Answer"},
            {"name": "Explanation"},
            {"name": "Image"},
        ],
        templates=[{
            "name": "Card 1",
            "qfmt": "<div class='question'>{{Question}}</div>",
            "afmt": """
<div class='question'>{{Question}}</div>
<hr>
<div class='answer'>{{Answer}}</div>
<div class='explanation'>{{Explanation}}</div>
<div class='image'>{{Image}}</div>
"""
        }],
        css="""
/*  ⬇  paste your full dark-theme CSS here  ⬇ */
.card {
  font-family: Arial;
  font-size: 26px;
  text-align: center;
  background-color: #1e1e1e;
  color: #ffffff;
}
.question { font-size: 28px; margin-bottom: 10px; }
.answer   { color: #4da6ff; font-weight: bold; margin: 8px 0; }
.explanation { color: #ff66cc; font-style: italic; margin-top: 10px; }
.image { color:#aaaaaa; font-size:18px; margin-top:12px; }
.card img {
  display:block; margin:0 auto;
  transform:scale(.60); transform-origin:center;
  transition:transform .3s ease-in-out; cursor:pointer;
}
.card img:hover { transform:scale(1); }
"""
    )

    deck = Deck(20504900110, deck_name)
    for c in cards:
        note = Note(
            model=model,
            fields=[
                c["question"],
                c["answer"],
                f"{c['explanation']} (Slide {c['slide_number']})",
                c["image"],
            ],
        )
        deck.add_note(note)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck, media_files=media_files).write_to_file(tmp.name)

    return send_file(tmp.name, as_attachment=True, download_name=f"{deck_name}.apkg")


# ── healthcheck ───────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return "Server is running ✅", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
