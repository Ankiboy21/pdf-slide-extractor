import os, io, json, tempfile, logging
from itertools import chain

import fitz                              # PyMuPDF
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package
from PIL import Image

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

IMAGE_FOLDER         = "images"
SCOPES               = ["https://www.googleapis.com/auth/drive.readonly"]
SERVICE_ACCOUNT_FILE = "credentials/drive_service_account.json"
MEDIA_EXTS           = (".png", ".jpg", ".jpeg")


def get_drive_service():
    if os.path.exists(SERVICE_ACCOUNT_FILE):
        creds = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES
        )
    else:
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


def find_matching_folder_for_pdf(pdf_file_id: str):
    service   = get_drive_service()
    meta      = service.files().get(fileId=pdf_file_id, fields="name,parents").execute()
    pdf_name  = meta["name"]
    folder_nm = pdf_name[:-4] if pdf_name.lower().endswith(".pdf") else pdf_name
    parent_id = meta["parents"][0]

    logging.info(f"Searching parent folder {parent_id} for subfolder '{folder_nm}'")
    qry = (
        f"'{parent_id}' in parents and "
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{folder_nm}' and trashed=false"
    )
    resp = service.files().list(q=qry, fields="files(id,name)").execute().get("files", [])
    if not resp:
        logging.warning("No matching subfolder found")
        return None

    folder_id = resp[0]["id"]
    logging.info(f"Auto-detected image folder: {resp[0]['name']} ({folder_id})")
    return folder_id


def download_images_from_drive(folder_id: str, dest_folder: str):
    if not DRIVE_AVAILABLE:
        return []

    service = get_drive_service()
    os.makedirs(dest_folder, exist_ok=True)

    qry   = f"'{folder_id}' in parents and mimeType contains 'image/' and trashed=false"
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

        try:
            img = Image.open(path)
            img.thumbnail((1024, 1024))
            img.save(path, format="JPEG", quality=70)
            logging.info(f"Optimized image: {name}")
        except Exception as e:
            logging.warning(f"Could not optimize {name}: {e}")

        downloaded.append(path)

    return downloaded


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


@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    data = request.get_json(silent=True) or {}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return jsonify({"error": "Bad JSON"}), 400

    # Unpack payload
    if isinstance(data, list):
        raw_cards, deck_name = data, "Lecture Deck"
        drive_folder_id, lecture_file_id = None, None
    else:
        raw_cards        = data.get("cards", [])
        deck_name        = data.get("deck_name", "Lecture Deck")
        drive_folder_id  = data.get("image_folder_drive_id")
        lecture_file_id  = data.get("lecture_file_drive_id")
        if not drive_folder_id and lecture_file_id and DRIVE_AVAILABLE:
            drive_folder_id = find_matching_folder_for_pdf(lecture_file_id)

    if raw_cards and isinstance(raw_cards[0], list):
        raw_cards = list(chain.from_iterable(raw_cards))
    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    # Download images if folder set
    media_files = []
    if drive_folder_id and DRIVE_AVAILABLE:
        tmp_drive_folder = f"/tmp/{deck_name.replace(' ', '_')}"
        media_files.extend(download_images_from_drive(drive_folder_id, tmp_drive_folder))

    # Parse cards + image matching
    cards = []
    for idx, c in enumerate(raw_cards, 1):
        slide_no = c.get("slide_number", idx)
        img_tag  = ""
        # try direct <img> filename or suffix match...
        # (omitted here for brevity, same as your logic)
        cards.append({
            "question":    c.get("question")    or c.get("Question")    or f"Card {idx}",
            "answer":      c.get("answer")      or c.get("Answer")      or "",
            "explanation": c.get("explanation") or c.get("Explanation") or "",
            "image":       img_tag,
        })

    # Build Anki deck
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
/* Your CSS here */
"""
    )

    deck = Deck(20504900110, deck_name)
    for note_data in cards:
        note = Note(
            model=model,
            fields=[
                note_data["question"],
                note_data["answer"],
                note_data["explanation"],
                note_data["image"],
            ]
        )
        deck.add_note(note)

    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck, media_files=media_files).write_to_file(tmpf.name)

    # <<<<<< FIXED send_file with mimetype >>>>>>
    return send_file(
        tmpf.name,
        as_attachment=True,
        download_name=f"{deck_name}.apkg",
        mimetype="application/octet-stream"
    )


@app.route("/", methods=["GET"])
def home():
    return "Server is running ✅", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
