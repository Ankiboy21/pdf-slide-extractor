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

IMAGE_FOLDER = "images"                            # local fallback
SCOPES       = ["https://www.googleapis.com/auth/drive.readonly"]
SERVICE_ACCOUNT_FILE = "credentials/drive_service_account.json"
MEDIA_EXTS   = (".png", ".jpg", ".jpeg")

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
                "Google Drive service account key not found: "
                "neither file nor SERVICE_ACCOUNT_JSON env var is set."
            )
        info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=SCOPES
        )
    return build("drive", "v3", credentials=creds)

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
    else:
        raw_cards = data.get("cards") or data.get("Array") or []
        deck_name = data.get("deck_name", "Lecture Deck")

    if raw_cards and isinstance(raw_cards[0], list):
        raw_cards = list(chain.from_iterable(raw_cards))
    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    # Build cards without images, support multiple slide numbers
    cards = []
    for idx, c in enumerate(raw_cards, 1):
        # Extract core fields
        question    = c.get("question")    or c.get("Question") or f"Card {idx}"
        answer      = c.get("answer")      or c.get("Answer")   or ""
        explanation = c.get("explanation") or c.get("Explanation") or ""

        # Slide number logic: int or list
        sn = c.get("slide_number")
        if isinstance(sn, list):
            slide_label = "Slides " + ", ".join(str(n) for n in sn)
        elif isinstance(sn, int):
            slide_label = f"Slide {sn}"
        else:
            slide_label = f"Slide {idx}"

        # Append slide info to explanation
        full_expl = f"{explanation} ({slide_label})"
        cards.append((question, answer, full_expl))

    # Define Anki model (no image field)
    model = Model(
        1607392319,
        "Styled Lecture Model",
        fields=[
            {"name": "Question"},
            {"name": "Answer"},
            {"name": "Explanation"},
        ],
        templates=[{
            "name": "Card 1",
            "qfmt": "<div class='question'>{{Question}}</div>",
            "afmt": """
<div class='question'>{{Question}}</div>
<hr>
<div class='answer'>{{Answer}}</div>
<div class='explanation'>{{Explanation}}</div>
"""
        }],
        css="""
.card { font-family:Arial; font-size:26px; text-align:center; background:#1e1e1e; color:#fff; }
.question { font-size:28px; margin-bottom:10px; }
.answer   { color:#4da6ff; font-weight:bold; margin:8px 0; }
.explanation { color:#ff66cc; font-style:italic; margin-top:10px; }
"""
    )

    # Create deck and add notes
    deck = Deck(20504900110, deck_name)
    for q, a, full_expl in cards:
        note = Note(
            model=model,
            fields=[q, a, full_expl]
        )
        deck.add_note(note)

    # Write and return the .apkg
    tmpf = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck).write_to_file(tmpf.name)
    return send_file(tmpf.name, as_attachment=True, download_name=f"{deck_name}.apkg")

@app.route("/", methods=["GET"])
def home():
    return "Server is running ✅", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
