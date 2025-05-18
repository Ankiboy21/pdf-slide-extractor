import os, io, json, tempfile, logging
from itertools import chain

import fitz                              # PyMuPDF
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

IMAGE_FOLDER = "images"                            # local fallback
SCOPES       = ["https://www.googleapis.com/auth/drive.readonly"]
SERVICE_ACCOUNT_FILE = "credentials/drive_service_account.json"
MEDIA_EXTS   = (".png", ".jpg", ".jpeg")


# ────────────────────────────────────────────────────────────
# Helper: get an authorized Drive service
# ────────────────────────────────────────────────────────────
def get_drive_service():
    # 1) If you’ve uploaded a file in /credentials, use it
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    else:
        # 2) Otherwise read your JSON key from an env var
        sa_json = os.environ.get("SERVICE_ACCOUNT_JSON")
        if not sa_json:
            raise RuntimeError(
                "Google Drive service account key not found: "
                "neither file nor SERVICE_ACCOUNT_JSON env var is set."
            )
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )

    return build("drive", "v3", credentials=creds)


# ────────────────────────────────────────────────────────────
# Helper: find the image-folder that matches a PDF in the same parent
# ────────────────────────────────────────────────────────────
def find_matching_folder_for_pdf(pdf_file_id: str):
    service = get_drive_service()
    # 1) fetch the PDF's metadata
    meta = service.files().get(
        fileId=pdf_file_id, fields="name,parents"
    ).execute()
    pdf_name = meta["name"]
    # remove trailing .pdf if present
    folder_name = pdf_name[:-4] if pdf_name.lower().endswith(".pdf") else pdf_name
    parent_id = meta["parents"][0]

    logging.info(f"Searching parent folder {parent_id} for subfolder named '{folder_name}'")
    # 2) search for a folder with that name
    qry = (
        f"'{parent_id}' in parents and "
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{folder_name}' and trashed=false"
    )
    resp = service.files().list(q=qry, fields="files(id,name)").execute()
    matches = resp.get("files", [])
    if not matches:
        logging.warning("No matching subfolder found")
        return None

    folder_id = matches[0]["id"]
    logging.info(f"Auto-detected image folder: {matches[0]['name']} ({folder_id})")
    return folder_id


# ────────────────────────────────────────────────────────────
# Helper: download all images from a Drive folder
# ────────────────────────────────────────────────────────────
def download_images_from_drive(folder_id: str, dest_folder: str):
    if not DRIVE_AVAILABLE:
        return []

    service = get_drive_service()
    os.makedirs(dest_folder, exist_ok=True)

    qry = (
        f"'{folder_id}' in parents "
        "and mimeType contains 'image/' "
        "and trashed=false"
    )
    files = service.files().list(q=qry, fields="files(id,name)").execute().get("files", [])
    logging.info(f"Found {len(files)} images in Drive folder {folder_id}")

    downloaded = []
    for f in files:
        name = f["name"]
        if not name.lower().endswith(MEDIA_EXTS):
            continue

        path = os.path.join(dest_folder, name)
        logging.info(f"Downloading {name} → {path}")
        req = service.files().get_media(fileId=f["id"])
        with io.FileIO(path, "wb") as fh:
            dl = MediaIoBaseDownload(fh, req)
            done = False
            while not done:
                _, done = dl.next_chunk()

        downloaded.append(path)

    return downloaded


# ── extract-text endpoint (unchanged) ─────────────────────────
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
        for i, page in enumerate(doc, start=1)
        if page.get_text().strip()
    ]

    doc.close()
    os.remove(tmp.name)
    return jsonify({"slides": slides})


# ── generate-apkg endpoint (auto folder detection) ──────────────
@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    data = request.get_json(silent=True)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return jsonify({"error": "Bad JSON"}), 400

    if not isinstance(data, (list, dict)):
        return jsonify({"error": "Body must be a list or object"}), 400

    # Unpack incoming payload
    if isinstance(data, list):
        raw_cards, deck_name = data, "Lecture Deck"
        drive_folder_id = None
        lecture_file_id = None
    else:
        raw_cards = data.get("cards") or data.get("Array") or []
        deck_name = data.get("deck_name", "Lecture Deck")
        drive_folder_id   = data.get("image_folder_drive_id")
        lecture_file_id   = data.get("lecture_file_drive_id")

        # ── AUTO-DETECT the image folder if none provided ──
        if not drive_folder_id and lecture_file_id and DRIVE_AVAILABLE:
            drive_folder_id = find_matching_folder_for_pdf(lecture_file_id)

    if raw_cards and isinstance(raw_cards[0], list):
        raw_cards = list(chain.from_iterable(raw_cards))
    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    # ── 1) Download images from Drive if we have a folder ──────
    tmp_drive_folder = ""
    media_files = []
    if drive_folder_id and DRIVE_AVAILABLE:
        tmp_drive_folder = f"/tmp/{deck_name.replace(' ', '_')}"
        media_files.extend(download_images_from_drive(drive_folder_id, tmp_drive_folder))

    # ── 2) Build cards[] and match each slide’s image ─────────
    cards = []
    for idx, c in enumerate(raw_cards, 1):
        slide_no = c.get("slide_number", idx)
        img_tag  = c.get("image") or c.get("Image") or ""
        logging.info(f"Card {idx}: slide_number={slide_no}, img_tag={img_tag}")

        # Extract explicit filename if provided
        fname = ""
        if "<img src='" in img_tag:
            try:
                fname = img_tag.split("<img src='")[1].split("'")[0]
            except IndexError:
                fname = ""

        matched = None
        # A) direct match by filename
        if fname:
            pd = os.path.join(tmp_drive_folder, fname) if tmp_drive_folder else ""
            pl = os.path.join(IMAGE_FOLDER, fname)
            if pd and os.path.exists(pd):
                matched = pd
            elif os.path.exists(pl):
                matched = pl

        # B) suffix match by slide number (00001, 00002, etc.)
        if not matched:
            suffix = f"-{str(slide_no).zfill(5)}.jpg"
            for folder in (tmp_drive_folder, IMAGE_FOLDER):
                if folder and os.path.isdir(folder):
                    for f_name in os.listdir(folder):
                        if f_name.lower().endswith(suffix):
                            matched = os.path.join(folder, f_name)
                            break
                    if matched:
                        break

        if matched:
            logging.info(f"Matched image: {matched}")
            media_files.append(matched)

        cards.append({
            "question":    c.get("question")    or c.get("Question") or f"Card {idx}",
            "answer":      c.get("answer")      or c.get("Answer")   or "",
            "explanation": c.get("explanation") or c.get("Explanation") or "",
            "image":       img_tag,
            "slide_number": slide_no,
        })

    logging.info(f"Final media_files: {media_files}")

    # ── 3) Build the Anki deck ──────────────────────────────────
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
/* Your existing CSS: dark theme, centered, hover-zoom on img */
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
    for note_data in cards:
        note = Note(
            model=model,
            fields=[
                note_data["question"],
                note_data["answer"],
                f"{note_data['explanation']} (Slide {note_data['slide_number']})",
                note_data["image"],
            ]
        )
        deck.add_note(note)

    # ── 4) Write and return the .apkg ─────────────────────────
    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck, media_files=media_files).write_to_file(tmpf.name)
    return send_file(tmpf.name, as_attachment=True, download_name=f"{deck_name}.apkg")


@app.route("/", methods=["GET"])
def home():
    return "Server is running ✅", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
