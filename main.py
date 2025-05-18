import os, io, json, tempfile, logging
from itertools import chain

import fitz                          # PyMuPDF
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package

# ── Try to import Google Drive API; if missing, skip Drive logic ──
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    DRIVE_AVAILABLE = True
except ImportError:
    DRIVE_AVAILABLE = False

# ── Configuration ─────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
IMAGE_FOLDER = "images"
SCOPES       = ["https://www.googleapis.com/auth/drive.readonly"]
SERVICE_ACCOUNT_FILE = "credentials/drive_service_account.json"
MEDIA_EXTS   = (".png", ".jpg", ".jpeg")


# ── Helper: download images from Drive ─────────────────────────────
def download_images_from_drive(folder_id: str, dest_folder: str):
    logging.info(f"Drive SDK {'available' if DRIVE_AVAILABLE else 'unavailable'}, "
                 f"folder_id={folder_id}")
    if not DRIVE_AVAILABLE:
        return []

    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    service = build("drive", "v3", credentials=creds)
    os.makedirs(dest_folder, exist_ok=True)

    query = (
        f"'{folder_id}' in parents and mimeType contains 'image/' and trashed = false"
    )
    files = service.files().list(q=query, fields="files(id, name)").execute().get("files", [])
    logging.info(f"Found {len(files)} files in Drive folder {folder_id}")

    downloaded = []
    for f in files:
        name = f["name"]
        if not name.lower().endswith(MEDIA_EXTS):
            logging.info(f"Skipping non-image: {name}")
            continue

        path = os.path.join(dest_folder, name)
        logging.info(f"Downloading {name} → {path}")
        request = service.files().get_media(fileId=f["id"])
        with io.FileIO(path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        downloaded.append(path)

    logging.info(f"Downloaded images: {downloaded}")
    return downloaded


# ── /extract-text endpoint (unchanged) ─────────────────────────────
@app.route("/extract-text", methods=["POST"])
def extract_text():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    uploaded = request.files["file"]
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        uploaded.save(tmp.name)
        doc = fitz.open(tmp.name)

    slides = []
    for i, page in enumerate(doc, start=1):
        txt = page.get_text().strip()
        if txt:
            slides.append({"slide_number": i, "text": txt})

    doc.close()
    os.remove(tmp.name)
    return jsonify({"slides": slides})


# ── /generate-apkg endpoint (instrumented) ─────────────────────────
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
        raw_cards, deck_name, drive_id = data, "Lecture Deck", None
    else:
        raw_cards = data.get("cards") or data.get("Array") or []
        deck_name = data.get("deck_name", "Lecture Deck")
        drive_id  = data.get("image_folder_drive_id")

    if raw_cards and isinstance(raw_cards[0], list):
        raw_cards = list(chain.from_iterable(raw_cards))
    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    # 1) Download images if requested
    tmp_drive_folder = ""
    media_files = []
    if drive_id:
        tmp_drive_folder = f"/tmp/{deck_name.replace(' ', '_')}"
        downloaded = download_images_from_drive(drive_id, tmp_drive_folder)
        media_files.extend(downloaded)

    # 2) Parse cards & match images
    cards = []
    for idx, c in enumerate(raw_cards, 1):
        slide_no = c.get("slide_number") or idx
        img_tag  = c.get("image") or c.get("Image") or ""
        logging.info(f"Processing card {idx}, slide_number={slide_no}, img_tag={img_tag}")

        # explicit filename
        fname = ""
        if "<img src='" in img_tag:
            try:
                fname = img_tag.split("<img src='")[1].split("'")[0]
            except IndexError:
                fname = ""
        logging.info(f"Extracted filename: {fname}")

        matched = None
        # A) direct match
        if fname:
            pd = os.path.join(tmp_drive_folder, fname) if tmp_drive_folder else ""
            pl = os.path.join(IMAGE_FOLDER, fname)
            if pd and os.path.exists(pd):
                matched = pd
            elif os.path.exists(pl):
                matched = pl
            logging.info(f"Direct match: {matched}")

        # B) suffix match
        if not matched:
            suffix = f"-{str(slide_no).zfill(5)}.jpg"
            logging.info(f"Looking for suffix: {suffix}")
            if tmp_drive_folder and os.path.isdir(tmp_drive_folder):
                for f in os.listdir(tmp_drive_folder):
                    if f.lower().endswith(suffix):
                        matched = os.path.join(tmp_drive_folder, f)
                        break
            if not matched and os.path.isdir(IMAGE_FOLDER):
                for f in os.listdir(IMAGE_FOLDER):
                    if f.lower().endswith(suffix):
                        matched = os.path.join(IMAGE_FOLDER, f)
                        break
            logging.info(f"Suffix match: {matched}")

        if matched:
            media_files.append(matched)

        cards.append({
            "question":    c.get("question")    or c.get("Question")    or f"Card {idx}",
            "answer":      c.get("answer")      or c.get("Answer")      or "",
            "explanation": c.get("explanation") or c.get("Explanation") or "",
            "image":       img_tag,
            "slide_number": slide_no,
        })

    logging.info(f"Final media_files list: {media_files}")

    # 3) Build Anki deck
    model = Model(
        1607392319,
        "Styled Lecture Model",
        fields=[{"name":"Question"},{"name":"Answer"},{"name":"Explanation"},{"name":"Image"}],
        templates=[{
            "name":"Card 1",
            "qfmt":"<div class='question'>{{Question}}</div>",
            "afmt":"""
<div class='question'>{{Question}}</div>
<hr>
<div class='answer'>{{Answer}}</div>
<div class='explanation'>{{Explanation}}</div>
<div class='image'>{{Image}}</div>
"""
        }],
        css="""/* your CSS here */"""
    )
    deck = Deck(20504900110, deck_name)
    for c in cards:
        note = Note(model=model, fields=[
            c["question"],
            c["answer"],
            f"{c['explanation']} (Slide {c['slide_number']})",
            c["image"],
        ])
        deck.add_note(note)

    # 4) Write .apkg
    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck, media_files=media_files).write_to_file(tmpf.name)
    return send_file(tmpf.name, as_attachment=True, download_name=f"{deck_name}.apkg")


@app.route("/", methods=["GET"])
def home():
    return "Server is running ✅", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
