from flask import Flask, request, jsonify, send_file, Response
import requests
from datetime import datetime
import io
from flask_cors import CORS
from dotenv import load_dotenv
import os
import google.generativeai as genai
from flask_pymongo import PyMongo
from bson import ObjectId
from datetime import datetime, timedelta
import fitz
import json
import time

load_dotenv() 

app = Flask(__name__)
CORS(app)
app.config['CORS_HEADERS'] = 'Content-Type'

# Configure Gemini API
GEMINI_API_KEY=os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("models/gemini-1.5-flash-latest")

# Connect MongoDB
MONGODB_URL = os.getenv("MONGODB_URL")
app.config["MONGO_URI"] = MONGODB_URL
mongo = PyMongo(app)

sessions_collection = mongo.db.sessions

# Test mongodb connection
@app.route("/mongo-test")
def mongo_test():
    try:
        count = mongo.db.sessions.count_documents({})
        return f"Connected to MongoDB! Session count: {count}"
    except Exception as e:
        return f"MongoDB connection failed: {str(e)}", 500

def get_available_models():
    try:
        res = requests.get("http://localhost:11434/api/tags", timeout=5)
        return sorted(set(m['name'].split(":")[0] for m in res.json().get("models", [])))
    except:
        return []

@app.route("/models")
def models():
    local_models=get_available_models()
    cloud_models=["gemini"]
    return jsonify({
        "local_models": local_models,
        "cloud_models": cloud_models,
    })

@app.route("/select_model", methods=["POST"])
def select_model():
    global current_model
    current_model = request.json.get("model", "phi3")
    return jsonify({"status": "ok"})

@app.route("/chat", methods=["POST"])
def chat():
    try:
        # ====== Base form data ======
        user_msg = request.form.get("message", "")
        model_type = request.form.get("model_type", "")
        model_name = request.form.get("model_name", "")
        session_id = request.form.get("session_id", "1")
        session_name = request.form.get("session_name", "")
        user_timestamp = datetime.now() - timedelta(seconds=10)

        # Fetch current session history
        current_session_history = ""
        if session_id != "1":  # If not a new session
            try:
                current_session = mongo.db.sessions.find_one({"_id": ObjectId(session_id)})
                if current_session and current_session.get("messages"):
                    for msg in current_session["messages"]:
                        current_session_history += f"{msg['role']}: {msg['content']}\n"
            except Exception as e:
                print(f"Error fetching current session history: {e}")

        # Mentions: fetch context from other sessions
        mention_session_ids = request.form.getlist("mention_session_ids[]")
        mention_history_context = ""
        if mention_session_ids:
            print(mention_session_ids)
            for m_id in mention_session_ids:
                if ObjectId.is_valid(m_id):
                    s = mongo.db.sessions.find_one({"_id": ObjectId(m_id)})
                    if s:
                        for m in s.get("messages", []):
                            mention_history_context += f"{m['role']}: {m['content']}\n"

        # Combine all context
        combined_input = user_msg
        if current_session_history or mention_history_context:
            context_parts = []
            if current_session_history:
                context_parts.append(f"Current conversation history:\n{current_session_history}")
            if mention_history_context:
                context_parts.append(f"Referenced conversation context:\n{mention_history_context}")
            
            combined_input = (
                f"Here is the conversation context:\n"
                f"{''.join(context_parts)}\n"
                f"Now, based on the above context, here is the user's new message:\n"
                f"{user_msg}"
            )
        
        # ====== File Handling (optional) ======
        uploaded_file = request.files.get("uploaded_file")
        if uploaded_file:
            if not allowed_file(uploaded_file.filename):
                return jsonify({"error": "Unsupported file type"}), 400
            if uploaded_file.filename == "":
                return jsonify({"error": "Empty file"}), 400

            file_bytes = uploaded_file.read()
            file_ext = uploaded_file.filename.rsplit(".", 1)[-1].lower()

            if model_type == "local":
                return jsonify({"error": "Selected local model does not support files"}), 400
            else:
                # Preprocess file for Gemini
                if file_ext == "pdf":
                    extracted_text = extract_text_from_pdf_bytes(file_bytes)
                    combined_input = f"{combined_input}\n\n[PDF Content Extracted]\n{extracted_text}"
                else:
                    # For image/video/etc, handle as media input
                    # Here gemini_model accepts both text + media
                    response = gemini_model.generate_content([
                        combined_input,
                        {"mime_type": uploaded_file.mimetype or "image/jpeg", "data": file_bytes}
                    ])
                    latency_ms = 0
                    bot_reply = response.text or "No reply."
                    # Save to DB (with uploaded_file info)
                    return save_and_return(session_id, session_name, model_name, user_msg, bot_reply, uploaded_file, file_bytes)

        # ====== Model Handling (text only or text+mentions) ======
        bot_reply = "No reply."
        latency_ms = 0
        if model_type == "local":
            payload = {
                "model": model_name,
                "prompt": combined_input,
                "stream": False,
            }
            try:
                latency_ms = datetime.now()
                response = requests.post("http://localhost:11434/api/generate", json=payload, timeout=60)
                latency_ms = int((datetime.now() - latency_ms).total_seconds() * 1000)
                bot_reply = response.json().get("response", "No reply.")
            except Exception as e:
                bot_reply = f"Local model error: {str(e)}"
        else:
            try:
                if model_name == "gemini":
                    print(combined_input)
                    latency_ms = datetime.now()
                    response = gemini_model.generate_content(combined_input)
                    latency_ms = int((datetime.now() - latency_ms).total_seconds() * 1000)
                    bot_reply = response.text or "No Reply"
            except Exception as e:
                bot_reply = f"Cloud model error: {str(e)}"

        # ====== Message Format ======
        messages = [
            {"role": "user", "content": user_msg, "timestamp": user_timestamp},
            {"role": "bot", "content": bot_reply, "timestamp": datetime.now(), "model_name": model_name}
        ]

        # ====== Store in DB ======
        if session_id != "1":
            mongo.db.sessions.update_one(
                {"_id": ObjectId(session_id)},
                {"$push": {"messages": {"$each": messages}}},
            )
        else:
            session_doc = {
                "session_name": session_name or "How can I help you?",
                "messages": messages,
                "created_at": datetime.now(),
            }
            inserted = mongo.db.sessions.insert_one(session_doc)
            session_id = str(inserted.inserted_id)

        return jsonify({
            "response": bot_reply,
            "session_id": session_id,
            "timestamp": messages[1]["timestamp"].isoformat(),
            "latency": latency_ms
        })

    except Exception as e:
        print("Error in /chat:", e)
        return jsonify({"error": str(e)}), 500


@app.route("/chat/stream", methods=["POST"])
def chat_stream():
    try:
        # ====== Base form data ======
        user_msg = request.form.get("message", "")
        model_type = request.form.get("model_type", "")
        model_name = request.form.get("model_name", "")
        session_id = request.form.get("session_id", "1")
        session_name = request.form.get("session_name", "")
        user_timestamp = datetime.now() - timedelta(seconds=10)

        # Fetch current session history
        current_session_history = ""
        if session_id != "1":  # If not a new session
            try:
                current_session = mongo.db.sessions.find_one({"_id": ObjectId(session_id)})
                if current_session and current_session.get("messages"):
                    for msg in current_session["messages"]:
                        current_session_history += f"{msg['role']}: {msg['content']}\n"
            except Exception as e:
                print(f"Error fetching current session history: {e}")

        # Mentions: fetch context from other sessions
        mention_session_ids = request.form.getlist("mention_session_ids[]")
        mention_history_context = ""
        if mention_session_ids:
            print(mention_session_ids)
            for m_id in mention_session_ids:
                if ObjectId.is_valid(m_id):
                    s = mongo.db.sessions.find_one({"_id": ObjectId(m_id)})
                    if s:
                        for m in s.get("messages", []):
                            mention_history_context += f"{m['role']}: {m['content']}\n"

        # Combine all context
        combined_input = user_msg
        if current_session_history or mention_history_context:
            context_parts = []
            if current_session_history:
                context_parts.append(f"Current conversation history:\n{current_session_history}")
            if mention_history_context:
                context_parts.append(f"Referenced conversation context:\n{mention_history_context}")
            
            combined_input = (
                f"Here is the conversation context:\n"
                f"{''.join(context_parts)}\n"
                f"Now, based on the above context, here is the user's new message:\n"
                f"{user_msg}"
            )
        
        # ====== File Handling (optional) ======
        uploaded_file = request.files.get("uploaded_file")
        if uploaded_file:
            if not allowed_file(uploaded_file.filename):
                return jsonify({"error": "Unsupported file type"}), 400
            if uploaded_file.filename == "":
                return jsonify({"error": "Empty file"}), 400

            file_bytes = uploaded_file.read()
            file_ext = uploaded_file.filename.rsplit(".", 1)[-1].lower()

            if model_type == "local":
                return jsonify({"error": "Selected local model does not support files"}), 400
            else:
                # For file uploads, we'll use non-streaming for now
                if file_ext == "pdf":
                    extracted_text = extract_text_from_pdf_bytes(file_bytes)
                    combined_input = f"{combined_input}\n\n[PDF Content Extracted]\n{extracted_text}"
                else:
                    response = gemini_model.generate_content([
                        combined_input,
                        {"mime_type": uploaded_file.mimetype or "image/jpeg", "data": file_bytes}
                    ])
                    bot_reply = response.text or "No reply."
                    return save_and_return(session_id, session_name, model_name, user_msg, bot_reply, uploaded_file, file_bytes)

        def generate_stream():
            bot_reply = ""
            start_time = datetime.now()
            
            # Send session info first
            yield f"data: {json.dumps({'type': 'session_info', 'session_id': session_id})}\n\n"
            
            try:
                if model_type == "local":
                    payload = {
                        "model": model_name,
                        "prompt": combined_input,
                        "stream": True,
                    }
                    
                    response = requests.post("http://localhost:11434/api/generate", json=payload, stream=True, timeout=60)
                    response.raise_for_status()
                    
                    for line in response.iter_lines():
                        if line:
                            try:
                                chunk_data = json.loads(line.decode('utf-8'))
                                chunk_text = chunk_data.get("response", "")
                                if chunk_text:
                                    bot_reply += chunk_text
                                    yield f"data: {json.dumps({'type': 'chunk', 'text': chunk_text})}\n\n"
                                
                                if chunk_data.get("done", False):
                                    break
                            except json.JSONDecodeError:
                                continue
                            except GeneratorExit:
                                # Handle client disconnect/stop generation
                                break
                                
                else:  # Cloud model (Gemini)
                    if model_name == "gemini":
                        # Gemini streaming
                        response = gemini_model.generate_content(
                            combined_input,
                            stream=True
                        )
                        
                        for chunk in response:
                            try:
                                chunk_text = chunk.text if chunk.text else ""
                                if chunk_text:
                                    bot_reply += chunk_text
                                    yield f"data: {json.dumps({'type': 'chunk', 'text': chunk_text})}\n\n"
                            except GeneratorExit:
                                # Handle client disconnect/stop generation
                                break
                    
            except Exception as e:
                error_msg = f"Error: {str(e)}"
                bot_reply = error_msg
                yield f"data: {json.dumps({'type': 'error', 'message': error_msg})}\n\n"
            
            # Calculate latency
            end_time = datetime.now()
            latency_ms = int((end_time - start_time).total_seconds() * 1000)
            
            # Save to database only if we have some content
            if bot_reply.strip():
                messages = [
                    {"role": "user", "content": user_msg, "timestamp": user_timestamp},
                    {"role": "bot", "content": bot_reply, "timestamp": end_time, "model_name": model_name}
                ]

                final_session_id = session_id
                if session_id != "1":
                    mongo.db.sessions.update_one(
                        {"_id": ObjectId(session_id)},
                        {"$push": {"messages": {"$each": messages}}},
                    )
                else:
                    session_doc = {
                        "session_name": session_name or "How can I help you?",
                        "messages": messages,
                        "created_at": datetime.now(),
                    }
                    inserted = mongo.db.sessions.insert_one(session_doc)
                    final_session_id = str(inserted.inserted_id)
                
                # Send completion message
                yield f"data: {json.dumps({'type': 'complete', 'session_id': final_session_id, 'timestamp': end_time.isoformat(), 'latency': latency_ms})}\n\n"

        return Response(
            generate_stream(),
            mimetype='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Headers': 'Cache-Control'
            }
        )

    except Exception as e:
        print("Error in /chat/stream:", e)
        return jsonify({"error": str(e)}), 500


def save_and_return(session_id, session_name, model_name, user_msg, bot_reply, uploaded_file, file_bytes):
    """Helper for media case to store and return."""
    messages = [
        {
            "role": "user",
            "content": user_msg,
            "timestamp": datetime.now() - timedelta(seconds=10),
            "uploaded_file": {
                "name": uploaded_file.filename,
                "type": uploaded_file.mimetype,
                "size": len(file_bytes),
            },
        },
        {
            "role": "bot",
            "content": bot_reply,
            "timestamp": datetime.now(),
            "model_name": model_name,
        }
    ]
    if session_id != "1":
        mongo.db.sessions.update_one(
            {"_id": ObjectId(session_id)},
            {
                "$push": {"messages": {"$each": messages}},
                "$set": {"session_name": session_name or "How can I help you?"}
            },
        )
    else:
        session_doc = {
            "messages": messages,
            "created_at": datetime.now(),
            "session_name": session_name or "How can I help you?",
        }
        inserted = mongo.db.sessions.insert_one(session_doc)
        session_id = str(inserted.inserted_id)

    return jsonify({
        "response": bot_reply,
        "session_id": session_id,
        "timestamp": messages[1]["timestamp"].isoformat(),
        "latency": 0
    })

@app.route("/chat/history", methods=["POST"])
def chat_history():
    data = request.json or {}
    id_list = data.get("session_ids", [])
    
    try:
        object_ids = [ObjectId(sid) for sid in id_list]
    except Exception as e:
        return jsonify({"error": "Invalid session ID format"}), 400

    sessions = mongo.db.sessions.find({"_id": {"$in": object_ids}}).sort("created_at", -1)

    result = []
    for session in sessions:
        session["_id"] = str(session["_id"])
        for msg in session.get("messages", []):
            msg["timestamp"] = msg["timestamp"].isoformat()
        result.append(session)
    
    return jsonify(result)

@app.route("/chat/<session_id>", methods=["GET"])
def get_session_messages(session_id):
    try:
        session = mongo.db.sessions.find_one({"_id": ObjectId(session_id)})

        if not session:
            return jsonify({"error": "Session not found"}), 404

        # Convert timestamps to ISO format for JSON serialization
        for msg in session.get("messages", []):
            if "timestamp" in msg:
                msg["timestamp"] = msg["timestamp"].isoformat()

        return jsonify({
            "session_id": str(session["_id"]),
            "messages": session["messages"]
        })

    except Exception as e:
        return jsonify({"error": f"Invalid session ID: {str(e)}"}), 400

@app.route("/chat/rename", methods=["POST"])
def rename_session():
    data = request.json or {}
    session_id = data.get("session_id")
    new_name = data.get("new_name")
    
    if not session_id or not new_name:
        return jsonify({"error": "Missing session_id or new_name"}), 400

    try:
        result = mongo.db.sessions.update_one(
            {"_id": ObjectId(session_id)},
            {"$set": {"session_name": new_name}}
        )

        if result.matched_count == 0:
            return jsonify({"error": "Session not found"}), 404

        return jsonify({"message": "Session renamed successfully"})

    except Exception as e:
        return jsonify({"error": f"Failed to rename session: {str(e)}"}), 500
    
@app.route("/clear", methods=["POST"])
def clear():
    data = request.get_json()
    session_id = data.get("session_id")

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    try:
        result = mongo.db.sessions.update_one(
            {"_id": ObjectId(session_id)},
            {"$set": {"messages": []}}
        )

        if result.matched_count == 0:
            return jsonify({"error": "Session not found"}), 404

        return jsonify({"status": "cleared", "session_id": session_id})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

for m in genai.list_models():
    print(m.name, m.supported_generation_methods)

# Allowed image extensions
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'mp4', 'pdf', 'mp3'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_pdf_bytes(file_bytes: bytes) -> str:
    text = ""
    with fitz.open(stream=file_bytes, filetype="pdf") as doc:
        for page in doc:
            text += page.get_text()
            text += "\n\n"
    return text.strip()

@app.route("/chat/delete/<session_id>", methods=["DELETE"])
def delete_chat(session_id):
    try:
        # Validate session_id
        if not ObjectId.is_valid(session_id):
            return jsonify({"error": "Invalid session_id"}), 400

        # Attempt to delete
        result = mongo.db.sessions.delete_one({"_id": ObjectId(session_id)})

        if result.deleted_count == 0:
            return jsonify({"error": "Chat session not found"}), 404

        return jsonify({"status": "success", "message": "Chat deleted successfully"})
    except Exception as e:
        print("Error in /chat/delete:", e)
        return jsonify({"error": str(e)}), 500
    
if __name__ == "__main__":
    app.run(debug=True)