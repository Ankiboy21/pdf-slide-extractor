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


# --- Step 3: Accept GPT-generated flashcards + build Anki deck ---
@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    data = request.get_json()
    if not data or "slides" not in data:
        return jsonify({"error": "Missing slide data"}), 400

    slides = data["slides"]

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

    deck = Deck(20504900110, "Generated Deck")

    for slide in slides:
        question = f"What is on slide {slide['slide_number']}?"
        answer = f"{slide['flashcard']}<br><br><i>(Slide {slide['slide_number']})</i>"

        note = Note(model=anki_model, fields=[question, answer])
        deck.add_note(note)

    temp_apkg = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck).write_to_file(temp_apkg.name)

    return send_file(temp_apkg.name, as_attachment=True, download_name="flashcards.apkg")

# --- Run app ---
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)

