print("Starting Flask app...")
from flask import Flask, render_template, request, redirect, url_for, flash, session
import os
from werkzeug.utils import secure_filename
import PyPDF2
import pytesseract
from pdf2image import convert_from_path
import uuid
import psycopg2
from dotenv import load_dotenv
from tax_calculator import calculate_old_regime, calculate_new_regime
import requests

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'pdf'}

app = Flask(__name__, template_folder='templates')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.secret_key = 'supersecretkey'  # For flash messages

load_dotenv()
DB_URL = os.getenv('DB_URL')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        if 'pdf' not in request.files:
            flash('No file part')
            print("No file part in request.")
            return redirect(request.url)
        file = request.files['pdf']
        if file.filename == '':
            flash('No selected file')
            print("No file selected.")
            return redirect(request.url)
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            print(f"PDF uploaded: {filepath}")
            extracted_data = extract_pdf_data(filepath)
            print(f"Extracted data: {extracted_data}")
            structured_data = gemini_structuring_stub(extracted_data)
            print(f"Structured data: {structured_data}")
            return render_template('form.html', data=structured_data)
        else:
            flash('Invalid file type. Only PDF allowed.')
            print("Invalid file type uploaded.")
            return redirect(request.url)
    return render_template('upload.html')

@app.route('/advisor', methods=['GET', 'POST'])
def advisor():
    # On GET, show Gemini's follow-up question
    if request.method == 'GET':
        # Use session or DB to get user data (for demo, use dummy or last form data)
        user_data = session.get('user_data', {})
        if not user_data:
            return redirect(url_for('index'))
        question = get_gemini_followup_question(user_data)
        session['advisor_question'] = question
        return render_template('ask.html', question=question, suggestions=None)
    # On POST, get user answer and show Gemini's suggestions
    else:
        user_answer = request.form.get('user_answer', '')
        question = session.get('advisor_question', '')
        user_data = session.get('user_data', {})
        suggestions = get_gemini_suggestions(user_data, question, user_answer)
        return render_template('ask.html', question=None, suggestions=suggestions)

def get_gemini_followup_question(user_data):
    import requests, json
    api_key = os.getenv("GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    prompt = (
        "Given the following user financial data, ask a smart, contextual follow-up question to help the user optimize their tax or investments. "
        "Be proactive and relevant.\n"
        f"User Data: {json.dumps(user_data)}"
    )
    data = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        print("Sending follow-up question prompt to Gemini:", prompt)
        response = requests.post(url, json=data, timeout=30)
        response.raise_for_status()
        result = response.json()
        print("Gemini follow-up question response:", result)
        text = result['candidates'][0]['content']['parts'][0]['text']
        return text.strip()
    except Exception as e:
        print("Gemini follow-up question failed:", e)
        return "What is your primary financial goal for this year?"

def get_gemini_suggestions(user_data, question, user_answer):
    import requests, json
    api_key = os.getenv("GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    prompt = (
        "Given the user's financial data, the following follow-up question, and the user's answer, "
        "provide 3 personalized, actionable investment and tax-saving suggestions in short, clear bullet points.\n"
        f"User Data: {json.dumps(user_data)}\n"
        f"Follow-up Question: {question}\n"
        f"User Answer: {user_answer}\n"
        "Return only the suggestions as a numbered or bulleted list."
    )
    data = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        print("Sending suggestions prompt to Gemini:", prompt)
        response = requests.post(url, json=data, timeout=30)
        response.raise_for_status()
        result = response.json()
        print("Gemini suggestions response:", result)
        text = result['candidates'][0]['content']['parts'][0]['text']
        # Split into suggestions (by line or bullet)
        suggestions = [s.strip('-•. ') for s in text.strip().split('\n') if s.strip()]
        return suggestions
    except Exception as e:
        print("Gemini suggestions failed:", e)
        return ["Invest in tax-saving instruments under Section 80C.", "Consider health insurance for 80D benefits.", "Review your HRA and rent receipts for maximum exemption."]

# Update the /save route to store user_data in session for advisor
@app.route('/save', methods=['POST'])
def save():
    data = {k: request.form.get(k, '') for k in [
        'gross_salary', 'basic_salary', 'hra_received', 'rent_paid',
        'deduction_80c', 'deduction_80d', 'standard_deduction',
        'professional_tax', 'tds', 'tax_regime',
        'tax_old_regime', 'tax_new_regime'  # Accept these if present
    ]}
    print("Form data received:", data)
    selected_regime = data.get('tax_regime', 'new')
    session_id = str(uuid.uuid4())
    session['user_data'] = data  # Store for advisor
    # Use Gemini's tax values if present, else calculate locally
    try:
        tax_old = float(data.get('tax_old_regime') or 0)
        tax_new = float(data.get('tax_new_regime') or 0)
        if tax_old != 0:
            print("Using Gemini's tax_old_regime value.")
        else:
            print("Gemini did not provide tax_old_regime. Using local calculation.")
            tax_old = calculate_old_regime(data)
        if tax_new != 0:
            print("Using Gemini's tax_new_regime value.")
        else:
            print("Gemini did not provide tax_new_regime. Using local calculation.")
            tax_new = calculate_new_regime(data)
    except Exception:
        print("Error parsing Gemini tax values. Using local calculation for both.")
        tax_old = calculate_old_regime(data)
        tax_new = calculate_new_regime(data)
    print(f"Tax Old Regime: {tax_old}, Tax New Regime: {tax_new}")
    best_regime = 'old' if tax_old < tax_new else 'new'

    # Save to Supabase
    try:
        print("Connecting to Supabase DB...")
        conn = psycopg2.connect(DB_URL)
        cur = conn.cursor()
        print("Inserting data into UserFinancials...")
        cur.execute('''
            INSERT INTO "UserFinancials" (
                session_id, gross_salary, basic_salary, hra_received, rent_paid,
                deduction_80c, deduction_80d, standard_deduction, professional_tax, tds
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (
            session_id, data['gross_salary'], data['basic_salary'], data['hra_received'],
            data['rent_paid'], data['deduction_80c'], data['deduction_80d'],
            data['standard_deduction'], data['professional_tax'], data['tds']
        ))
        cur.close()
        conn.commit()
        conn.close()
        print("Insert successful.")
    except Exception as e:
        print("Database error:", e)
        import traceback
        traceback.print_exc()
        flash(f"Error saving to database: {e}")

    return render_template('results.html',
        tax_old_regime=tax_old,
        tax_new_regime=tax_new,
        selected_regime=selected_regime,
        best_regime=best_regime,
        gross_salary=data.get('gross_salary', 0),
        basic_salary=data.get('basic_salary', 0),
        hra_received=data.get('hra_received', 0),
        deduction_80c=data.get('deduction_80c', 0)
    )

def extract_pdf_data(filepath):
    text = ""
    try:
        with open(filepath, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                text += page.extract_text() or ''
    except Exception:
        pass
    if not text.strip():
        try:
            images = convert_from_path(filepath)
            for img in images:
                text += pytesseract.image_to_string(img)
        except Exception:
            pass
    return {'raw_text': text}

def gemini_structuring_stub(extracted_data):
    import re
    import json
    text = extracted_data.get('raw_text', '')
    api_key = os.getenv("GEMINI_API_KEY")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    prompt = (
        "Extract the following fields from this salary slip or Form 16 text:\n"
        "gross_salary, basic_salary, hra_received, rent_paid, deduction_80c, deduction_80d, "
        "standard_deduction, professional_tax, tds.\n"
        "Then, calculate the annual tax for both the Old and New Regimes for FY 2024-25 as per Indian tax rules. "
        "Return the result as a valid JSON object with these keys (all keys must be present, all values must be numbers, even if zero):\n"
        "gross_salary, basic_salary, hra_received, rent_paid, deduction_80c, deduction_80d, standard_deduction, professional_tax, tds, tax_old_regime, tax_new_regime\n"
        "Important:\n"
        "- The tax values must be annual, not monthly.\n"
        "- If a value is missing, use 0.\n"
        "- Return only the JSON object, with no explanation or text outside the JSON.\n"
        "- Example output:\n"
        "{\n"
        "  \"gross_salary\": 960000,\n"
        "  \"basic_salary\": 480000,\n"
        "  \"hra_received\": 240000,\n"
        "  \"rent_paid\": 180000,\n"
        "  \"deduction_80c\": 150000,\n"
        "  \"deduction_80d\": 25000,\n"
        "  \"standard_deduction\": 50000,\n"
        "  \"professional_tax\": 2400,\n"
        "  \"tds\": 60000,\n"
        "  \"tax_old_regime\": 35000,\n"
        "  \"tax_new_regime\": 42000\n"
        "}\n\n"
        f"Text:\n{text}"
    )
    data = {
        "contents": [
            {"parts": [{"text": prompt}]}
        ]
    }
    try:
        print("Sending prompt to Gemini API...")
        print(prompt)
        response = requests.post(url, json=data, timeout=30)
        response.raise_for_status()
        result = response.json()
        print("Gemini API response:", result)
        gemini_text = result['candidates'][0]['content']['parts'][0]['text']
        match = re.search(r'({.*})', gemini_text, re.DOTALL)
        if match:
            print("Gemini extraction successful.")
            return json.loads(match.group(1))
        else:
            print("Gemini response did not contain JSON. Falling back to regex parser.")
    except Exception as e:
        print(f"Gemini extraction failed: {e}. Falling back to regex parser.")
        import traceback
        traceback.print_exc()
    # Fallback: regex parser (as before)
    def find_value(labels):
        for label in labels:
            match = re.search(rf"{label}[:\s]+([\d,]+)", text, re.IGNORECASE)
            if match:
                print(f"Found {label}: {match.group(1)}")
                return match.group(1).replace(',', '')
        print(f"No match for {labels}")
        return ''
    rent_paid = find_value(['Rent Paid', 'Rent'])
    if not rent_paid:
        rent_paid = find_value(['House Rent Allowance', 'HRA'])
    return {
        'gross_salary': find_value(['Gross Salary', 'Gross Income']),
        'basic_salary': find_value(['Basic Salary', 'Basic']),
        'hra_received': find_value(['HRA', 'House Rent Allowance']),
        'rent_paid': rent_paid,
        'deduction_80c': find_value(['80C', 'Section 80C', 'Deduction 80C']),
        'deduction_80d': find_value(['80D', 'Section 80D', 'Deduction 80D']),
        'standard_deduction': find_value(['Standard Deduction']),
        'professional_tax': find_value(['Professional Tax']),
        'tds': find_value(['TDS', 'Tax Deducted at Source']),
        'raw_text': text
    }

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=True) 