# app.py
import os
import subprocess
import logging
from datetime import datetime, timezone 
from io import BytesIO
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS 
from flask_sqlalchemy import SQLAlchemy 
from sqlalchemy.exc import SQLAlchemyError 

# Konfigurasi Logging
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# --- KONFIGURASI DATABASE ---
# Catatan: Di Vercel, SQLite mungkin tidak bekerja karena environment read-only.
# Jika deployment gagal, Anda harus beralih ke PostgreSQL/MySQL.
db_path = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'mental_health_history.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
CORS(app) 

# --- VERCEL CONFIG: Membuat tabel di startup ---
# Menjalankan database creation di startup context (diperlukan untuk serverless)
with app.app_context():
    db.create_all()

# --- MODEL DATA SQLAlchemy ---
class TesResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    name = db.Column(db.String(100), nullable=True)
    note = db.Column(db.String(255), nullable=True)
    total_score = db.Column(db.Integer, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    
    def __repr__(self):
        return f'<TesResult {self.id} Score: {self.total_score}>'


# --- LOGIKA KLASIFIKASI ---
def classify(total):
    """Menentukan kategori, saran, dan warna berdasarkan skor total (0-30)."""
    if not 0 <= total <= 30:
        logging.warning(f"Skor diluar rentang (0-30) terdeteksi: {total}. Disesuaikan ke batas terdekat.")
        total = max(0, min(30, total))
        
    if total <= 9:
        return {'cat': 'Baik', 'advice': 'Pertahankan pola hidup sehat, teruskan refleksi diri. Fokus pada kualitas tidur dan relasi sosial positif.', 'color': '#16a34a'}
    elif total <= 19:
        return {'cat': 'Perlu Perhatian Ringan', 'advice': 'Coba atur jadwal harian, fokus pada teknik relaksasi ringan, dan pastikan mendapat istirahat yang cukup. Kurangi begadang.', 'color': '#f59e0b'}
    else: 
        return {'cat': 'Disarankan Konsultasi', 'advice': 'Skor menunjukkan kebutuhan perhatian yang lebih besar. Pertimbangkan segera berkonsultasi dengan profesional kesehatan mental (psikolog/psikiater).', 'color': '#ef4444'}


# --- ROUTE API 1: Menyimpan Hasil Tes ---
@app.route('/api/save', methods=['POST'])
def calculate_and_save():
    data = request.json
    answers = data.get('answers', [])
    
    if len(answers) != 10:
        return jsonify({"error": "Harus ada 10 jawaban (0-3)."}), 400

    # 1. Persiapan Eksekusi C++ (Menggunakan nama executable di Linux/Vercel)
    cpp_executable = os.path.abspath("./logic_linux") 
    
    command = [cpp_executable] + [str(a) for a in answers]
    
    total_score = 0
    classification = {}

    try:
        # 2. Menjalankan C++
        # Gunakan timeout untuk mencegah proses C++ yang mandek
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5) 
        
        # Cek output C++
        output = result.stdout.strip()
        total_score = int(output)
        
        # Pengecekan rentang skor
        if not 0 <= total_score <= 30:
            app.logger.error(f"C++ output score out of range (0-30): {output}")
            return jsonify({"error": f"C++ mengembalikan skor total diluar rentang (0-30): {output}"}), 500

        classification = classify(total_score)
        
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() or "Kesalahan tidak diketahui dari logika C++."
        app.logger.error(f"C++ Execution Error: {error_msg}")
        # Tambahkan pesan untuk log Vercel jika eksekusi C++ gagal
        return jsonify({"error": f"Kesalahan saat menjalankan logika C++: {error_msg}. Pastikan logic_linux punya izin eksekusi."}), 500
        
    except subprocess.TimeoutExpired:
        app.logger.error("C++ process timed out.")
        return jsonify({"error": "Proses logika C++ melebihi batas waktu eksekusi (5 detik)."}), 500
        
    except ValueError:
        app.logger.error(f"C++ output invalid: '{output}'")
        return jsonify({"error": "C++ mengembalikan output yang tidak valid (bukan angka)."}), 500

    except Exception as e:
        app.logger.error(f"Unexpected error during C++ execution: {e}")
        return jsonify({"error": f"Kesalahan tak terduga: {e}"}), 500
        
    
    # 3. Penyimpanan ke Database 
    try:
        new_result = TesResult(
            name=data.get('name', '').strip(),
            note=data.get('note', '').strip(),
            total_score=total_score,
            category=classification['cat']
        )
        db.session.add(new_result)
        db.session.commit()
        
    except SQLAlchemyError as e:
        app.logger.error(f"DATABASE ERROR: Gagal commit ke SQLite: {e}")
        db.session.rollback()
        # Catatan: SQLite di Vercel Sering GAGAL di sini karena read-only filesystem.
        return jsonify({"error": "Gagal menyimpan data (kemungkinan file system Vercel read-only untuk SQLite)."}), 500
        
    except Exception as e:
        app.logger.error(f"Server Internal Error: {e}")
        db.session.rollback()
        return jsonify({"error": "Kesalahan server internal tidak terduga saat menyimpan data."}), 500


    # 4. Mengirimkan hasil kembali ke frontend
    return jsonify({
        "status": "success",
        "total_score": total_score,
        "category": classification['cat'],
        "advice": classification['advice'],
        "color": classification['color']
    })


# --- ROUTE API 2, 3, 4 (History, Clear, Export) Tetap Sama ---

@app.route('/api/history', methods=['GET'])
def get_history():
    # ... (body fungsi ini tetap sama)
    date_format = '%Y-%m-%d %H:%M:%S'
    
    results = TesResult.query.order_by(TesResult.timestamp.desc()).all()
    
    history_list = []
    for r in results:
        timestamp_str = r.timestamp.strftime(date_format) if r.timestamp else None
        
        history_list.append({
            'timestamp': timestamp_str,
            'name': r.name,
            'total': r.total_score,
            'category': r.category,
            'note': r.note
        })
        
    return jsonify(history_list)


@app.route('/api/clear_history', methods=['DELETE'])
def clear_history():
    # ... (body fungsi ini tetap sama)
    try:
        num_deleted = db.session.query(TesResult).delete() 
        db.session.commit()
        app.logger.info(f"Deleted {num_deleted} records from database.")
        return jsonify({"status": "success", "message": f"{num_deleted} riwayat berhasil dihapus."}), 200
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.error(f"DATABASE ERROR: Gagal menghapus riwayat: {e}")
        return jsonify({"status": "error", "error": "Gagal menghapus data dari database."}), 500


@app.route('/api/export_csv', methods=['GET'])
def export_csv():
    # ... (body fungsi ini tetap sama)
    results = TesResult.query.order_by(TesResult.timestamp.asc()).all()
    
    csv_data = ['"timestamp","name","total","category","note"']
    
    def clean_csv_field(s):
        if s is None:
            return ''
        safe_s = str(s).replace('"', '""').replace('\n', ' ')
        return f'"{safe_s}"'

    for r in results:
        timestamp_str = r.timestamp.strftime('%Y-%m-%d %H:%M:%S') if r.timestamp else ''
        
        row_fields = [
            clean_csv_field(timestamp_str),
            clean_csv_field(r.name),
            str(r.total_score), 
            clean_csv_field(r.category),
            clean_csv_field(r.note)
        ]
        
        csv_data.append(','.join(row_fields)) 

    csv_output = "\n".join(csv_data)
    
    buffer = BytesIO()
    buffer.write(csv_output.encode('utf-8'))
    buffer.seek(0)
    
    return send_file(
        buffer,
        mimetype='text/csv',
        as_attachment=True,
        download_name='mental_check_history.csv'
    )
