from flask import Flask, request, jsonify
import fitz  # PyMuPDF
import tempfile
import os

app = Flask(__name__)

@app.route('/extract-pdf', methods=['POST'])
def extract_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided"}), 400

    pdf_file = request.files['file']

    # Save file temporarily
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
        pdf_file.save(temp.name)
        doc = fitz.open(temp.name)

        slides = []
        for i, page in enumerate(doc, start=1):
            text = page.get_text().strip()
            if text:
                slides.append({
                    "slide_number": i,
                    "text": text
                })

        os.remove(temp.name)

    return jsonify({"slides": slides})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
