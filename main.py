import os, tempfile
from itertools import chain
import fitz
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package

app = Flask(__name__)

@app.route("/extract-text", methods=["POST"])
def extract_text():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    pdf = request.files["file"]

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        pdf.save(tmp.name)
        doc = fitz.open(tmp.name)

    slides = [
        {"slide_number": i, "text": p.get_text().strip()}
        for i, p in enumerate(doc, 1) if p.get_text().strip()
    ]
    doc.close(); os.remove(tmp.name)
    return jsonify({"slides": slides})


@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"error": "Body was not valid JSON"}), 400

    if isinstance(data, list):
        raw_cards, deck_name = data, "Lecture Deck"
    elif isinstance(data, dict):
        raw_cards = data.get("cards") or data.get("Array") or []
        deck_name = data.get("deck_name", "Lecture Deck")
    else:
        return jsonify({"error": "Bad payload shape"}), 400

    if raw_cards and isinstance(raw_cards[0], list):
        raw_cards = list(chain.from_iterable(raw_cards))
    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    cards = []
    for idx, c in enumerate(raw_cards, 1):
        cards.append({
            "question": c.get("question") or c.get("Question") or f"Card {idx}",
            "answer": c.get("answer") or c.get("Answer") or "",
            "explanation": c.get("explanation") or c.get("Explanation") or "",
            "slide_number": c.get("slide_number") or idx
        })

    model = Model(
        1607392319, "Simple Model",
        fields=[{"name": "Question"}, {"name": "Answer"}],
        templates=[{
            "name": "Card 1",
            "qfmt": "{{Question}}",
            "afmt": "{{FrontSide}}<hr id=answer>{{Answer}}"
        }]
    )
    deck = Deck(20504900110, deck_name)
    for c in cards:
        ans = f"{c['answer']}<br><br><i>{c['explanation']}</i> (Slide {c['slide_number']})"
        deck.add_note(Note(model=model, fields=[c["question"], ans]))

    tmp_pkg = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck).write_to_file(tmp_pkg.name)
    return send_file(tmp_pkg.name, as_attachment=True, download_name=f"{deck_name}.apkg")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
