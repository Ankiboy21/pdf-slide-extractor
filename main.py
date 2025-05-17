import os
import tempfile
import fitz  # PyMuPDF
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package

app = Flask(__name__)

# --- Step 1: Extract slide text only (NO OpenAI calls here) ---
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
            slides.append({
                "slide_number": i,
                "text": slide_text
            })

    doc.close()
    os.remove(tmp_file.name)

    return jsonify({"slides": slides})


# --- Step 3: Accept GPT-generated flashcards (flat array) + build Anki deck ---
@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    # force=True lets Flask parse raw JSON strings if Make sends them as text
    data = request.get_json(force=True)

    cards = data.get("cards")
    if not cards:
        return jsonify({"error": "Missing cards"}), 400

    deck_name = data.get("deck_name", "Lecture Deck")

    anki_model = Model(
        1607392319,
        "Simple Model",
        fields=[{"name": "Question"}, {"name": "Answer"}],
        templates=[{
            "name": "Card 1",
            "qfmt": "{{Question}}",
            "afmt": "{{FrontSide}}<hr id=answer>{{Answer}}"
        }]
    )

    deck = Deck(20504900110, deck_name)

    # Add every card from the flat list
    for idx, card in enumerate(cards, start=1):
        question = card.get("question", f"Card {idx}")
        answer   = card.get("answer", "")
        expl     = card.get("explanation", "")
        slide_no = card.get("slide_number", idx)

        answer_field = f"{answer}<br><br><i>{expl}</i> (Slide {slide_no})"
        note = Note(model=anki_model, fields=[question, answer_field])
        deck.add_note(note)

    # Export deck
    temp_apkg = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck).write_to_file(temp_apkg.name)

    # Return the file
    return send_file(
        temp_apkg.name,
        as_attachment=True,
        download_name=f"{deck_name}.apkg"
    )

# --- Run app ---
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)

