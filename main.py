from flask import Flask, request, jsonify
import fitz  # PyMuPDF
import tempfile
import os
import openai

openai.api_key = os.getenv("OPENAI_API_KEY")  # Set this in Render environment settings

app = Flask(__name__)

def generate_flashcard(slide_text):
    prompt = f"""
    Turn the following slide text into a single high-quality Anki flashcard.

    Slide content:
    """
    {slide_text}
    """

    Format strictly as:
    question: ...
    answer: ...
    explanation: ...
    """

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7
        )
        content = response.choices[0].message["content"]
        parts = {line.split(":")[0].strip(): ":".join(line.split(":")[1:]).strip() for line in content.split("\n") if ":" in line}
        return {
            "question": parts.get("question", ""),
            "answer": parts.get("answer", ""),
            "explanation": parts.get("explanation", "")
        }
    except Exception as e:
        return {
            "question": "Error generating card.",
            "answer": str(e),
            "explanation": "OpenAI request failed."
        }

@app.route('/extract-pdf', methods=['POST'])
def extract_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    pdf_file = request.files['file']

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
        pdf_file.save(temp.name)
        doc = fitz.open(temp.name)

        cards = []
        for i, page in enumerate(doc, start=1):
            text = page.get_text().strip()
            if text:
                card = generate_flashcard(text)
                card["slide_number"] = i
                cards.append(card)

        os.remove(temp.name)

    return jsonify({"cards": cards})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
