import os
import json
import tempfile
from itertools import chain
import fitz
from flask import Flask, request, jsonify, send_file
from genanki import Model, Note, Deck, Package

app = Flask(__name__)

@app.route("/generate-apkg", methods=["POST"])
def generate_apkg():
    # Try to parse incoming JSON (object or string)
    data = request.get_json(silent=True)

    # ✅ If Make sends stringified JSON, decode it
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return jsonify({"error": "Could not decode stringified JSON"}), 400

    # ❌ No JSON received at all
    if data is None:
        return jsonify({"error": "Body was not valid JSON"}), 400

    # ✅ Accept top-level list OR object with cards/Array keys
    if isinstance(data, list):
        raw_cards, deck_name = data, "Lecture Deck"
    elif isinstance(data, dict):
        raw_cards = data.get("cards") or data.get("Array") or []
        deck_name = data.get("deck_name", "Lecture Deck")
    else:
        return jsonify({"error": "Bad payload shape"}), 400

    # ✅ Flatten nested arrays if needed
    if raw_cards and isinstance(raw_cards[0], list):
        raw_cards = list(chain.from_iterable(raw_cards))

    if not raw_cards:
        return jsonify({"error": "Missing cards"}), 400

    # ✅ Normalize keys
    cards = []
    for idx, c in enumerate(raw_cards, 1):
        cards.append({
            "question": c.get("question") or c.get("Question") or f"Card {idx}",
            "answer": c.get("answer") or c.get("Answer") or "",
            "explanation": c.get("explanation") or c.get("Explanation") or "",
            "slide_number": c.get("slide_number") or idx
        })

    # ✅ Build Anki model and deck
    model = Model(
        1607392319,
        "Simple Model",
        fields=[
            {"name": "Question"},
            {"name": "Answer"}
        ],
        templates=[
            {
                "name": "Card 1",
                "qfmt": "{{Question}}",
                "afmt": "{{FrontSide}}<hr id=answer>{{Answer}}"
            }
        ]
    )

    deck = Deck(20504900110, deck_name)

    for c in cards:
        ans = f"{c['answer']}<br><br><i>{c['explanation']}</i> (Slide {c['slide_number']})"
        deck.add_note(Note(model=model, fields=[c["question"], ans]))

    tmp_pkg = tempfile.NamedTemporaryFile(delete=False, suffix=".apkg")
    Package(deck).write_to_file(tmp_pkg.name)

    return send_file(tmp_pkg.name, as_attachment=True, download_name=f"{deck_name}.apkg")
