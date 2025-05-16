import os
import tempfile
import fitz  # PyMuPDF
import openai

from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package

openai.api_key = os.environ.get("OPENAI_API_KEY")

app = Flask(__name__)

# Anki model
model = Model(
    1607392319,
    "Simple Model",
    fields=[{"name": "Question"}, {"name": "Answer"}],
    templates=[{
        "name": "Card 1",
        "qfmt": "{{Question}}",
        "afmt": "{{FrontSide}}<hr id=answer>{{Answer}}"
    }]
)

# Create .apkg file from flashcards
def create_anki_deck(slides, deck_name="Generated Deck"):
    deck = Deck(20504900110, deck_name)
    for slide in slides:
        question = f"What is on slide {slide['slide_number']}?"
        answer = f"{slide['flashcard']}<br><br><i>(Slide {slide['slide_number']})</i>"
        note = Note(model=model, fields=[question, answer])
        deck.add_note(note)

    temp_apkg = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck).write_to_file(temp_apkg.name)
    return temp_apkg.name

# Generate flashcard using OpenAI GPT-4o
def generate_flashcard_from_text(text):
    prompt = f"""Generate a concise flashcard based on the following lecture slide text. Focus on clarity and important facts.

Slide Content:
{text}"""

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that writes flashcards for Anki."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=300
    )

    return response.choices[0].message.content.strip()

# Upload endpoint
@app.route("/extract-pdf", methods=["POST"])
def extract_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    pdf_file = request.files['file']

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
        pdf_file.save(temp.name)
        doc = fitz.open(temp.name)

    slides = []
    for i, page in enumerate(doc, start=1):
        text = page.get_text().strip()
        if text:
            flashcard = generate_flashcard_from_text(text)
            slides.append({
                "slide_number": i,
                "flashcard": flashcard
            })

    doc.close()
    os.remove(temp.name)

    apkg_path = create_anki_deck(slides)
    return send_file(apkg_path, as_attachment=True, download_name="flashcards.apkg")

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
