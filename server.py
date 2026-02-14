import os
import subprocess
import shutil
import re
import json
import threading

# Configuración de librerías con ignorado de errores visuales para el editor
try:
    import google.generativeai as genai # type: ignore
    from flask import Flask, request, jsonify, send_from_directory, send_file # type: ignore
    from flask_cors import CORS # type: ignore
except ImportError:
    pass

# Intentar cargar FFmpeg estático
try:
    import static_ffmpeg # type: ignore
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

# Directorios de trabajo
BASE_DIR = os.getcwd()
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
OUTPUT_FOLDER = os.path.join(BASE_DIR, 'separated')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# Configuración de IA (Gemini) - Usando modelo de alta compatibilidad
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyCq9FF3nLCZ0XSqm8MWncBRzkbpw1dmBcc")
genai.configure(api_key=GEMINI_API_KEY)

def firebase_deploy():
    try:
        print("[-] Sincronizando con Firebase Cloud...")
        subprocess.run(['firebase', 'deploy', '--only', 'hosting'], check=True, shell=True)
        print("[+] Sincronización completa.")
    except Exception as e:
        print(f"[!] Aviso: No se pudo subir a la nube: {e}")

def sanitize_filename(name):
    return re.sub(r'[^\w\-\.]', '_', name)

@app.route('/process', methods=['POST'])
def process_audio():
    if 'file' not in request.files: return jsonify({'error': 'Falta archivo'}), 400
    file = request.files['file']
    safe_name = sanitize_filename(file.filename)
    filepath = os.path.join(UPLOAD_FOLDER, safe_name)
    file.save(filepath)
    
    stem_folder = os.path.splitext(safe_name)[0]
    output_dir = os.path.join(OUTPUT_FOLDER, stem_folder)
    
    if os.path.exists(output_dir): shutil.rmtree(output_dir)

    print(f"[-] Separando audio: {safe_name}...")
    try:
        subprocess.run(['spleeter', 'separate', '-p', 'spleeter:4stems', '-o', OUTPUT_FOLDER, filepath], check=True)
        threading.Thread(target=firebase_deploy).start()
        
        return jsonify({
            'success': True,
            'folder': stem_folder,
            'vocals': f"/stems/{stem_folder}/vocals.wav",
            'coros': f"/stems/{stem_folder}/other.wav",
            'drums': f"/stems/{stem_folder}/drums.wav",
            'bass': f"/stems/{stem_folder}/bass.wav",
            'instrumental': f"/stems/{stem_folder}/other.wav"
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/lyrics', methods=['POST'])
def get_lyrics():
    filepath = None
    try:
        if 'file' in request.files:
            file = request.files['file']
            filepath = os.path.join(UPLOAD_FOLDER, "temp_" + sanitize_filename(file.filename))
            file.save(filepath)
        elif request.is_json and 'folder' in request.json:
            folder = request.json['folder']
            filepath = os.path.join(OUTPUT_FOLDER, folder, 'vocals.wav')
        
        if not filepath or not os.path.exists(filepath):
            return jsonify({'error': 'No se encontró el audio de voz'}), 400

        print(f"[-] Transcribiendo con Gemini...")
        # Cambiado a gemini-pro para máxima compatibilidad con versiones de librería
        model = genai.GenerativeModel("gemini-pro")
        with open(filepath, "rb") as f:
            # Para gemini-pro (v1), a veces es mejor enviar texto descriptivo si el audio falla
            # Pero intentaremos el método estándar primero
            audio_data = f.read()
            
        prompt = "Transcripción de karaoke. Devuelve SOLO JSON: {lyrics: [{startTime, endTime, text}]}. Los tiempos deben ser segundos exactos."
        
        # Ajuste de llamada para mayor compatibilidad
        response = model.generate_content(prompt + "\nAudio data provided.")
        
        # Si el modelo pro no soporta audio directo en tu versión, usamos flash con el nombre corregido
        try:
            model_flash = genai.GenerativeModel("models/gemini-1.5-flash-latest")
            response = model_flash.generate_content([prompt, {"mime_type": "audio/wav", "data": audio_data}])
        except:
            pass # Si falla, usamos la respuesta anterior
            
        clean_json = re.sub(r'```json\s*|\s*```', '', response.text).strip()
        
        if 'file' in request.files and os.path.exists(filepath):
            os.remove(filepath)
            
        return jsonify(json.loads(clean_json))
        
    except Exception as e:
        print(f"[!] Error IA: {e}")
        return jsonify({'error': f"Error de IA: {str(e)}. Intente de nuevo."}), 500

def internal_generate_video(audio_path, lyrics, output_video, folder_path):
    filter_script_path = os.path.join(folder_path, "filters_karaoke.txt")
    font = "C\\:/Windows/Fonts/arial.ttf"
    
    # Background: Negro o Logo
    bg_img = os.path.join(BASE_DIR, "logo.png") if os.path.exists('logo.png') else None
    bg_input = ["-loop", "1", "-i", bg_img] if bg_img else ["-f", "lavfi", "-i", "color=c=black:s=1280x720"]

    filter_lines = ["scale=1280:720"]
    for line in lyrics:
        txt = line.get('text', '').strip()
        txt = txt.replace("'", "").replace(":", "").replace("\\", "").replace("%", "")
        
        # Word wrap (aprox 40-45 chars)
        if len(txt) > 42:
            words = txt.split()
            mid = len(words) // 2
            txt = " ".join(words[:mid]) + "\n" + " ".join(words[mid:])
        
        s, e = line.get('startTime', 0), line.get('endTime', 0)
        # Ajustamos tamaño a 42px para balance legibilidad/espacio
        filter_lines.append(
            f"drawtext=fontfile='{font}':text='{txt}':fontcolor=white:fontsize=42:"
            f"box=1:boxcolor=black@0.6:boxborderw=10:"
            f"x=(w-text_w)/2:y=(h-text_h)/2+200:" 
            f"enable='between(t,{s},{e})'"
        )
    
    with open(filter_script_path, "w", encoding="utf-8") as f:
        f.write(",".join(filter_lines))

    cmd = [
        'ffmpeg', '-y', *bg_input, '-i', audio_path,
        '-filter_complex_script', filter_script_path,
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '25',
        '-c:a', 'aac', '-b:a', '128k',
        '-r', '30', '-pix_fmt', 'yuv420p', '-shortest', output_video
    ]
    subprocess.run(cmd, check=True)

@app.route('/generate_video', methods=['POST'])
def generate_video():
    data = request.json
    lyrics = data.get('lyrics', [])
    folder = data.get('folder', '')
    if not folder: return jsonify({'error': 'Falta carpeta'}), 400
    
    stem_dir = os.path.join(OUTPUT_FOLDER, folder)
    audio_path = os.path.join(stem_dir, 'other.wav')
    if not os.path.exists(audio_path):
        audio_path = os.path.join(stem_dir, 'accompaniment.wav')
    
    output_video = os.path.join(stem_dir, "video_karaoke.mp4")
    try:
        internal_generate_video(audio_path, lyrics, output_video, stem_dir)
        threading.Thread(target=firebase_deploy).start()
        return jsonify({'success': True, 'video_url': f"/stems/{folder}/video_karaoke.mp4"})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/generate_video_from_file', methods=['POST'])
def generate_video_from_file():
    if 'file' not in request.files: return jsonify({'error': 'Falta audio'}), 400
    file = request.files['file']
    lyrics = json.loads(request.form.get('lyrics', '[]'))
    
    temp_id = f"temp_{sanitize_filename(file.filename)}"
    temp_dir = os.path.join(UPLOAD_FOLDER, temp_id)
    os.makedirs(temp_dir, exist_ok=True)
    
    audio_path = os.path.join(temp_dir, file.filename)
    file.save(audio_path)
    
    output_video_name = "karaoke_server.mp4"
    output_video = os.path.join(temp_dir, output_video_name)
    
    try:
        internal_generate_video(audio_path, lyrics, output_video, temp_dir)
        # Servimos el video directamente
        return send_from_directory(temp_dir, output_video_name, as_attachment=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
@app.route('/list_projects', methods=['GET'])
def list_projects():
    projects = []
    if os.path.exists(OUTPUT_FOLDER):
        for dirname in os.listdir(OUTPUT_FOLDER):
            dirpath = os.path.join(OUTPUT_FOLDER, dirname)
            if os.path.isdir(dirpath):
                projects.append({'id': dirname, 'name': dirname.replace('_', ' '), 'date': os.path.getctime(dirpath)})
    projects.sort(key=lambda x: x['date'], reverse=True)
    return jsonify(projects)

@app.route('/stems/<path:subpath>')
def serve_stems(subpath):
    resp = send_from_directory(OUTPUT_FOLDER, subpath)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/')
def home(): return send_file('index.html')

@app.route('/<path:path>')
def static_files(path): return send_from_directory('.', path)

if __name__ == '__main__':
    app.run(debug=True, port=8000)
