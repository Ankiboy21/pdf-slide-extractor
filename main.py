import os
import tempfile
from itertools import chain

import fitz  # PyMuPDF
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package

app = Flask(__name__)

# ----------------------------------------------------------------------
#  STEP 1: Extract slide text (unchanged)
# ----------------------------------------------------------------------
@app.route("/extract-text", methods=["POST"])
def extract_text():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    uploaded_file = request.files["file"]

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


# ----------------------------------------------------------------------
#  STEP 3: Accept GPT flashcards + build Anki deck  (PATCHED)
# ----------------------------------------------------------------------
@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    # force=True lets Flask parse raw JSON strings if Make ever sends them
    data = request.get_json(force=True)

    # Accept either "cards" (preferred) or "Array" (legacy) as the key
    raw_cards = data.get("cards") or data.get("Array")
    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    # If Make accidentally double-nests the list ( [[...]] ), flatten it
    if isinstance(raw_cards, list) and raw_cards and isinstance(raw_cards[0], list):
        raw_cards = list(chain.from_iterable(raw_cards))

    # --- NEW: normalise key-case so we can handle "Question" / "question" ---
    cards = []
    for idx, c in enumerate(raw_cards, start=1):
        cards.append(
            {
                "question": c.get("question") or c.get("Question") or f"Card {idx}",
                "answer": c.get("answer") or c.get("Answer") or "",
                "explanation": c.get("explanation") or c.get("Explanation") or "",
                "slide_number": c.get("slide_number") or c.get("Slide_number") or idx,
            }
        )

    deck_name = data.get("deck_name", "Lecture Deck")

    anki_model = Model(
        1607392319,
        "Simple Model",
        fields=[{"name": "Question"}, {"name": "Answer"}],
        templates=[
            {
                "name": "Card 1",
                "qfmt": "{{Question}}",
                "afmt": "{{FrontSide}}<hr id=answer>{{Answer}}",
            }
        ],
    )

    deck = Deck(20504900110, deck_name)

    for card in cards:
        answer_field = (
            f"{card['answer']}<br><br>"
            f"<i>{card['explanation']}</i> (Slide {card['slide_number']})"
        )
        note = Note(model=anki_model, fields=[card["question"], answer_field])
        deck.add_note(note)

    temp_apkg = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck).write_to_file(temp_apkg.name)

    return send_file(
        temp_apkg.name,
        as_attachment=True,
        download_name=f"{deck_name}.apkg",
    )


# ----------------------------------------------------------------------
#  Run app
# ----------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
