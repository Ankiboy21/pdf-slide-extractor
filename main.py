import os
import tempfile
import fitz  # PyMuPDF
import openai

from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package

# Initialize OpenAI key from environment variable
openai.api_key = os.environ.get("OPENAI_API_KEY")

# Initialize Flask app
app = Flask(__name__)

# Anki model definition
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

# Create Anki deck and write to .apkg file
def create_anki_deck(slides, deck_name="Generated Deck"):
    deck = Deck(20504900110, deck_name)
    for slide in slides:
        question = f"What is on slide {slide['slide_number']}?"
        answer = f"{slide['flashcard']}<br><br><i>(Slide {slide['slide_number']})</i>"
        note = Note(model=anki_model, fields=[question, answer])
        deck.add_note(note)

    temp_apkg = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck).write_to_file(temp_apkg.name)
    return temp_apkg.name

# Generate a flashcard from slide text using OpenAI
def generate_flashcard_from_text(text):
    prompt = f"""Generate a clear, high-yield flashcard based on the following lecture slide text. Include only the most important fact or concept.

Slide Content:
{text}"""

    response = openai.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a helpful assistant that writes Anki-style flashcards."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3,
        max_tokens=300
    )

    return response.choices[0].message.content.strip()

# Main upload endpoint
@app.route("/extract-pdf", methods=["POST"])
def extract_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    # Save uploaded file to temp
    uploaded_file = request.files['file']
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
        uploaded_file.save(tmp_file.name)
        doc = fitz.open(tmp_file.name)

    # Process slides
    slides = []
    for i, page in enumerate(doc, start=1):
        slide_text = page.get_text().strip()
        if slide_text:
            flashcard = generate_flashcard_from_text(slide_text)
            slides.append({
                "slide_number": i,
                "flashcard": flashcard
            })

    doc.close()
    os.remove(tmp_file.name)

    # Create Anki deck and return as .apkg
    apkg_path = create_anki_deck(slides)
    return send_file(apkg_path, as_attachment=True, download_name="flashcards.apkg")

# Run the app on Render or localhost
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
