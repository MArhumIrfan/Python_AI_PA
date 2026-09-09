import tkinter as tk
from tkinter import scrolledtext
import threading
import speech_recognition as sr
import pyttsx3
import datetime
import sqlite3
import re
import os
import shutil
import webbrowser
from dotenv import load_dotenv
from google import genai
import urllib.parse

load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# --- Database Management ---
def init_db():
    conn = sqlite3.connect("assistant_memory.db")
    conn.execute('''CREATE TABLE IF NOT EXISTS chat_history 
                    (id INTEGER PRIMARY KEY, role TEXT, message TEXT)''')
    conn.commit(); conn.close()

def log_interaction(role, message):
    conn = sqlite3.connect("assistant_memory.db")
    conn.execute("INSERT INTO chat_history (role, message) VALUES (?, ?)", (role, message))
    conn.commit(); conn.close()

def get_recent_context():
    conn = sqlite3.connect("assistant_memory.db")
    # Increased limit to 10 to accommodate the new system feedback logs
    rows = conn.execute("SELECT role, message FROM chat_history ORDER BY id DESC LIMIT 10").fetchall()
    conn.close()
    return "".join([f"{role.upper()}: {msg}\n" for role, msg in reversed(rows)])

# --- System Command Parser & Feedback Loop ---
def execute_system_commands(text):
    """Parses AI output, executes OS commands, and logs feedback to the DB."""
    feedback_logs = []

    # 1. Single Argument Commands (MKDIR, DELETE, OPEN_LINK, OPEN_MEDIA)
    single_args = re.findall(r'\[CMD:\s*([A-Z_]+),\s*(?:path|url):\s*"(.*?)"\]', text)
    for cmd, arg in single_args:
        try:
            if cmd == "MKDIR":
                os.makedirs(arg, exist_ok=True)
                feedback_logs.append(f"SUCCESS: Created folder '{arg}'")
            elif cmd == "DELETE":
                if os.path.isfile(arg): os.remove(arg)
                elif os.path.isdir(arg): shutil.rmtree(arg)
                feedback_logs.append(f"SUCCESS: Deleted '{arg}'")
            elif cmd == "OPEN_LINK":
                webbrowser.open(arg)
                feedback_logs.append(f"SUCCESS: Opened URL '{arg}'")
            elif cmd == "OPEN_MEDIA":
                os.startfile(arg)
                feedback_logs.append(f"SUCCESS: Opened file '{arg}'")
        except Exception as e:
            feedback_logs.append(f"ERROR ({cmd} on {arg}): {e}")

    # 2. Double Argument Commands (MOVE, COPY)
    double_args = re.findall(r'\[CMD:\s*([A-Z_]+),\s*src:\s*"(.*?)",\s*dst:\s*"(.*?)"\]', text)
    for cmd, src, dst in double_args:
        try:
            if cmd == "MOVE":
                shutil.move(src, dst)
                feedback_logs.append(f"SUCCESS: Moved '{src}' to '{dst}'")
            elif cmd == "COPY":
                if os.path.isdir(src): shutil.copytree(src, dst)
                else: shutil.copy2(src, dst)
                feedback_logs.append(f"SUCCESS: Copied '{src}' to '{dst}'")
        except Exception as e:
            feedback_logs.append(f"ERROR ({cmd} from {src} to {dst}): {e}")

    # 3. Rename Command
    renames = re.findall(r'\[CMD:\s*RENAME,\s*path:\s*"(.*?)",\s*new_name:\s*"(.*?)"\]', text)
    for path, new_name in renames:
        try:
            # os.rename requires the full path for both arguments in most cases
            dir_name = os.path.dirname(path)
            full_new_name = os.path.join(dir_name, new_name)
            os.rename(path, full_new_name)
            feedback_logs.append(f"SUCCESS: Renamed to '{new_name}'")
        except Exception as e:
            feedback_logs.append(f"ERROR (RENAME on {path}): {e}")

    # 4. File Creation
    match_name = re.search(r'\[FILE:\s*(.*?)\]', text)
    match_code = re.search(r'```(?:[a-zA-Z0-9\+]*\n)?(.*?)```', text, re.DOTALL)
    if match_name and match_code:
        filename, code_content = match_name.group(1).strip(), match_code.group(1).strip()
        try:
            with open(filename, 'w', encoding='utf-8') as f: f.write(code_content)
            feedback_logs.append(f"SUCCESS: Saved file '{filename}'")
        except Exception as e:
            feedback_logs.append(f"ERROR (FILE Creation for {filename}): {e}")

    # 5. WhatsApp Command
    whatsapp_cmds = re.findall(r'\[CMD:\s*WHATSAPP,\s*phone:\s*"(.*?)",\s*message:\s*"(.*?)"\]', text)
    for phone, msg in whatsapp_cmds:
        try:
            # Encode the message so spaces and symbols don't break the link
            encoded_msg = urllib.parse.quote(msg)
            
            # The whatsapp:// protocol opens the native Windows desktop app
            link = f"whatsapp://send?phone={phone}&text={encoded_msg}"
            webbrowser.open(link)
            
            feedback_logs.append(f"SUCCESS: Opened WhatsApp deep-link for {phone}")
        except Exception as e:
            feedback_logs.append(f"ERROR (WHATSAPP for {phone}): {e}")

    # Process Feedback Loop
    for feedback in feedback_logs:
        update_chat(f"*** SYSTEM LOG: {feedback} ***")
        log_interaction("system", feedback) # Log to DB so AI knows if it worked

# --- Core Logic ---
def stream_ai_response(prompt):
    try:
        # Dynamic OS Profiling
        user_profile = os.environ.get('USERPROFILE', 'C:\\')
        
        sys_instruct = (
            f"You are an agentic OS assistant running on Windows 11.\n"
            f"The user's home directory is: {user_profile}\n"
            f"CRITICAL: Always use double Windows backslashes (\\\\) for paths.\n\n"
            "To perform actions, you MUST output these exact tags:\n"
            "- Create folder: [CMD: MKDIR, path: \"...\"]\n"
            "- Delete: [CMD: DELETE, path: \"...\"]\n"
            "- Move: [CMD: MOVE, src: \"...\", dst: \"...\"]\n"
            "- Copy: [CMD: COPY, src: \"...\", dst: \"...\"]\n"
            "- Rename: [CMD: RENAME, path: \"...\", new_name: \"...\"]\n"
            "- Open link: [CMD: OPEN_LINK, url: \"https...\"]\n"
            "- Open media: [CMD: OPEN_MEDIA, path: \"...\"]\n"
            "- WhatsApp: [CMD: WHATSAPP, phone: \"+92...\", message: \"...\"]\n"
            "- Write code: [FILE: path\\\\filename.ext] followed by a ```code``` block.\n\n"
            "If the user asks to send a WhatsApp message but does not provide a phone number with a country code, you MUST ask them for it first before generating the tag.\n"
            "The system will execute your tags and log successes or errors back to the chat history."
        )
        
        full_prompt = f"{sys_instruct}\n\nHistory:\n{get_recent_context()}\nUSER: {prompt}"
        
        chat = client.chats.create(model="gemini-3.6-flash")
        response_stream = chat.send_message_stream(full_prompt)
        
        update_chat("Assistant: ", newline=False)
        full_response = ""
        
        for chunk in response_stream:
            full_response += chunk.text
            chat_box.config(state=tk.NORMAL)
            chat_box.insert(tk.END, chunk.text)
            chat_box.config(state=tk.DISABLED)
            chat_box.yview(tk.END)
            
        update_chat("") 
        log_interaction("assistant", full_response)
        
        # Execute and report back
        execute_system_commands(full_response)
        
        # Clean text for speech
        clean_text = re.sub(r'```.*?```', 'the requested code', full_response, flags=re.DOTALL)
        clean_text = re.sub(r'\[(?:FILE|CMD).*?\]', '', clean_text)
        threading.Thread(target=speak, args=(clean_text, True), daemon=True).start()
        
    except Exception as e:
        update_chat(f"\n[AI Error]: {e}")

def update_chat(msg, newline=True):
    chat_box.config(state=tk.NORMAL)
    chat_box.insert(tk.END, msg + ("\n\n" if newline else ""))
    chat_box.config(state=tk.DISABLED)
    chat_box.yview(tk.END)

def speak(text, skip_log=False):
    if not skip_log: 
        update_chat(f"Assistant: {text}")
        log_interaction("assistant", text)
    try:
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
    except: pass

def process_command(query):
    if not query or query == "none": return
    update_chat(f"You: {query}"); log_interaction("user", query)

    if 'time' in query:
        speak(f"The time is {datetime.datetime.now().strftime('%H:%M:%S')}")
    elif 'exit' in query or 'bye' in query:
        speak("Goodbye!"); root.quit()
    else:
        stream_ai_response(query)

def handle_text_input(event=None):
    q = text_input.get().strip().lower()
    text_input.delete(0, tk.END)
    if q: threading.Thread(target=process_command, args=(q,), daemon=True).start()

def handle_voice_input():
    def listen():
        r = sr.Recognizer()
        with sr.Microphone() as source:
            update_chat("System: Listening...")
            r.adjust_for_ambient_noise(source, duration=0.5)
            try:
                audio = r.listen(source, timeout=5, phrase_time_limit=15)
                update_chat("System: Recognizing...")
                process_command(r.recognize_google(audio, language='en-in').lower())
            except Exception: update_chat("System: No speech detected.")
    threading.Thread(target=listen, daemon=True).start()

# --- GUI Construction ---
init_db()
root = tk.Tk(); root.title("Agentic AI Assistant"); root.geometry("550x700")
chat_box = scrolledtext.ScrolledText(root, wrap=tk.WORD, state=tk.DISABLED, font=("Consolas", 10))
chat_box.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)

input_frame = tk.Frame(root)
input_frame.pack(padx=10, pady=10, fill=tk.X)
text_input = tk.Entry(input_frame, font=("Arial", 12))
text_input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
text_input.bind("<Return>", handle_text_input)

tk.Button(input_frame, text="Send", command=handle_text_input, bg="#4CAF50", fg="white").pack(side=tk.LEFT)
tk.Button(input_frame, text="🎤 Mic", command=handle_voice_input, bg="#2196F3", fg="white").pack(side=tk.LEFT, padx=(10, 0))

root.mainloop()
