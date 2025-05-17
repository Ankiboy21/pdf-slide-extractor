import os
import json
import tempfile
from itertools import chain
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package

app = Flask(__name__)

# --- Endpoint to extract slide text from PDF ---
@app.route("/extract-text", methods=["POST"])
def extract_text():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    uploaded_file = request.files['file']
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
        uploaded_file.save(tmp_file.name)
        doc = fitz.open(tmp_file.name)

    slides = []
    for i, page in enumerate(doc, start=1):
        slide_text = page.get_text().strip()
        if slide_text:
            slides.append({"slide_number": i, "text": slide_text})

    doc.close()
    os.remove(tmp_file.name)

    return jsonify({"slides": slides})

# --- Endpoint to generate .apkg Anki deck ---
@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    data = request.get_json(silent=True)

    # Handle stringified JSON if Make sends as string
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return jsonify({"error": "Could not decode stringified JSON"}), 400

    if data is None:
        return jsonify({"error": "Body was not valid JSON"}), 400

    # Accept array or object with cards
    if isinstance(data, list):
        raw_cards, deck_name = data, "Lecture Deck"
    elif isinstance(data, dict):
        raw_cards = data.get("cards") or data.get("Array") or []
        deck_name = data.get("deck_name", "Lecture Deck")
    else:
        return jsonify({"error": "Bad payload shape"}), 400

    # Flatten arrays if needed
    if raw_cards and isinstance(raw_cards[0], list):
        raw_cards = list(chain.from_iterable(raw_cards))

    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    # Normalize card keys
    cards = []
    for idx, c in enumerate(raw_cards, 1):
        cards.append({
            "question": c.get("question") or c.get("Question") or f"Card {idx}",
            "answer": c.get("answer") or c.get("Answer") or "",
            "explanation": c.get("explanation") or c.get("Explanation") or "",
            "image": c.get("image") or c.get("Image") or "",
            "slide_number": c.get("slide_number") or idx
        })

    # Define the Anki card model
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
.card {
    font-family: Arial;
    font-size: 26px;
    text-align: center;
    background-color: #1e1e1e;
    color: #ffffff;
}

.question {
    font-size: 28px;
    margin-bottom: 10px;
}

.answer {
    color: #4da6ff;
    font-weight: bold;
    margin: 8px 0;
}

.explanation {
    color: #ff66cc;
    font-style: italic;
    margin-top: 10px;
}

.mnemonic {
    color: #ff66cc;
    font-style: italic;
    margin-top: 6px;
}

.image {
    color: #aaaaaa;
    font-size: 18px;
    margin-top: 12px;
}

.card img {
  display: block;
  margin: 0 auto;
  transform: scale(0.60);
  transform-origin: center;
  transition: transform 0.3s ease-in-out;
  cursor: pointer;
}

.card img:hover {
  transform: scale(1);
}
"""
    )

    # Create the Anki deck
    deck = Deck(20504900110, deck_name)
    for c in cards:
        answer_field = c['answer']
        explanation_field = f"{c['explanation']} (Slide {c['slide_number']})"
        note = Note(model=model, fields=[c["question"], answer_field, explanation_field, c["image"]])
        deck.add_note(note)

    tmp_apkg = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck).write_to_file(tmp_apkg.name)

    return send_file(tmp_apkg.name, as_attachment=True, download_name=f"{deck_name}.apkg")


# --- Optional route to test if server is running ---
@app.route("/", methods=["GET"])
def home():
    return "Server is running ✅", 200


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
