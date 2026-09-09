import time
import tkinter as tk
from tkinter import scrolledtext, font
import threading
import speech_recognition as sr
import pyttsx3
import datetime
import sqlite3
import re
import os
import shutil
import webbrowser
import urllib.parse
import pyaudio
import wave
from dotenv import load_dotenv
from google import genai


load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

# --- Global State Variables ---
is_recording = False
audio_frames = []
pyaudio_instance = None
audio_stream = None
stop_speaking_flag = False  # New flag to interrupt the AI

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
    rows = conn.execute("SELECT role, message FROM chat_history ORDER BY id DESC LIMIT 10").fetchall()
    conn.close()
    return "".join([f"{role.upper()}: {msg}\n" for role, msg in reversed(rows)])

# --- System Command Parser ---
def execute_system_commands(text):
    feedback_logs = []

    single_args = re.findall(r'\[CMD:\s*([A-Z_]+),\s*(?:path|url):\s*"(.*?)"\]', text)
    for cmd, arg in single_args:
        try:
            if cmd == "MKDIR": os.makedirs(arg, exist_ok=True); feedback_logs.append(f"SUCCESS: Created folder '{arg}'")
            elif cmd == "DELETE":
                if os.path.isfile(arg): os.remove(arg)
                elif os.path.isdir(arg): shutil.rmtree(arg)
                feedback_logs.append(f"SUCCESS: Deleted '{arg}'")
            elif cmd == "OPEN_LINK": webbrowser.open(arg); feedback_logs.append(f"SUCCESS: Opened URL '{arg}'")
            elif cmd == "OPEN_MEDIA": os.startfile(arg); feedback_logs.append(f"SUCCESS: Opened file '{arg}'")
        except Exception as e: feedback_logs.append(f"ERROR ({cmd} on {arg}): {e}")

    double_args = re.findall(r'\[CMD:\s*([A-Z_]+),\s*src:\s*"(.*?)",\s*dst:\s*"(.*?)"\]', text)
    for cmd, src, dst in double_args:
        try:
            if cmd == "MOVE": shutil.move(src, dst); feedback_logs.append(f"SUCCESS: Moved '{src}' to '{dst}'")
            elif cmd == "COPY":
                if os.path.isdir(src): shutil.copytree(src, dst)
                else: shutil.copy2(src, dst)
                feedback_logs.append(f"SUCCESS: Copied '{src}' to '{dst}'")
        except Exception as e: feedback_logs.append(f"ERROR ({cmd}): {e}")

    whatsapp_cmds = re.findall(r'\[CMD:\s*WHATSAPP,\s*phone:\s*"(.*?)",\s*message:\s*"(.*?)"\]', text)
    for phone, msg in whatsapp_cmds:
        try:
            link = f"whatsapp://send?phone={phone}&text={urllib.parse.quote(msg)}"
            webbrowser.open(link)
            feedback_logs.append(f"SUCCESS: Opened WhatsApp for {phone}")
        except Exception as e: feedback_logs.append(f"ERROR (WHATSAPP): {e}")

    match_name = re.search(r'\[FILE:\s*(.*?)\]', text)
    match_code = re.search(r'```(?:[a-zA-Z0-9\+]*\n)?(.*?)```', text, re.DOTALL)
    if match_name and match_code:
        filename, code_content = match_name.group(1).strip(), match_code.group(1).strip()
        try:
            with open(filename, 'w', encoding='utf-8') as f: f.write(code_content)
            feedback_logs.append(f"SUCCESS: Saved file '{filename}'")
        except Exception as e: feedback_logs.append(f"ERROR (FILE): {e}")

    for feedback in feedback_logs:
        safe_update_chat(f"⚙️ {feedback}")
        log_interaction("system", feedback)

# --- Core Logic ---
def stream_ai_response(prompt):
    user_profile = os.environ.get('USERPROFILE', 'C:\\')
    sys_instruct = (
        f"You are an agentic OS assistant running on Windows 11.\n"
        f"User's home directory: {user_profile}\n"
        f"Use double Windows backslashes (\\\\) for paths.\n\n"
        "Tags:\n"
        "- [CMD: MKDIR, path: \"...\"]\n"
        "- [CMD: DELETE, path: \"...\"]\n"
        "- [CMD: MOVE, src: \"...\", dst: \"...\"]\n"
        "- [CMD: COPY, src: \"...\", dst: \"...\"]\n"
        "- [CMD: OPEN_LINK, url: \"https...\"]\n"
        "- [CMD: OPEN_MEDIA, path: \"...\"]\n"
        "- [CMD: WHATSAPP, phone: \"+1...\", message: \"...\"]\n"
        "- [FILE: path\\\\name.ext] + ```code```\n"
    )
    full_prompt = f"{sys_instruct}\n\nHistory:\n{get_recent_context()}\nUSER: {prompt}"
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            chat = client.chats.create(model="gemini-3.6-flash")
            response_stream = chat.send_message_stream(full_prompt)
            
            safe_update_chat("🤖 Assistant: ", newline=False)
            full_response = ""
            
            for chunk in response_stream:
                full_response += chunk.text
                
                # Clean up the UI formatting
                ui_text = chunk.text
                ui_text = re.sub(r'^\s*[\*\-]\s+', '➔ ', ui_text, flags=re.MULTILINE)
                
                # Safely insert into Tkinter from this background thread
                root.after(0, lambda t=ui_text: __insert_text(t))
                
            safe_update_chat("") 
            log_interaction("assistant", full_response)
            execute_system_commands(full_response)
            
            threading.Thread(target=speak, args=(full_response,), daemon=True).start()
            
            # If successful, break out of the retry loop
            break 
            
        except Exception as e:
            error_message = str(e)
            if "503" in error_message and attempt < max_retries - 1:
                safe_update_chat(f"⚙️ System: Servers are busy. Retrying... (Attempt {attempt + 2}/{max_retries})")
                time.sleep(2) # Wait 2 seconds before trying again
            # Handle 429 Quota Exhausted
            elif "429" in error_message:
                # Extract the wait time if the API provides it, otherwise default to 60 seconds
                wait_time_match = re.search(r'retry in (\d+)s', error_message)
                wait_seconds = wait_time_match.group(1) if wait_time_match else "60"
                
                safe_update_chat(f"⚠️ System: Free tier API limit reached! Please wait {wait_seconds} seconds before sending another command.")
                break  
            else:
                safe_update_chat(f"\n⚠️ [AI Error]: {e}")
                break # Break on any other error (like a bad API key) or if out of retries
# --- Thread-Safe GUI Updaters ---
def __insert_text(text):
    """Internal function strictly for the main Tkinter thread."""
    chat_box.config(state=tk.NORMAL)
    chat_box.insert(tk.END, text)
    chat_box.config(state=tk.DISABLED)
    chat_box.yview(tk.END)

def safe_update_chat(msg, newline=True):
    """Schedules a chat update safely on the main UI thread."""
    formatted_msg = msg + ("\n\n" if newline else "")
    root.after(0, lambda: __insert_text(formatted_msg))

# --- Intelligent Speaking Logic ---
def speak(text):
    global stop_speaking_flag
    stop_speaking_flag = False
    
    # 1. Clean the text for TTS (Remove code blocks, tags, and markdown symbols)
    clean_text = re.sub(r'```.*?```', 'the requested code', text, flags=re.DOTALL)
    clean_text = re.sub(r'\[(?:FILE|CMD).*?\]', '', clean_text)
    clean_text = re.sub(r'[*#_~`]', '', clean_text)  # Strips markdown characters completely
    
    # 2. Split into chunks/sentences so we can interrupt it mid-paragraph
    # Splits by period, exclamation, or question mark followed by a space
    sentences = [s.strip() for s in re.split(r'(?<=[.!?]) +', clean_text) if s.strip()]
    
    try:
        engine = pyttsx3.init()
        for sentence in sentences:
            if stop_speaking_flag:
                break # Abort speaking if user interrupted
            engine.say(sentence)
            engine.runAndWait()
    except: pass

def interrupt_ai(event=None):
    """Triggers the flag to shut the AI up."""
    global stop_speaking_flag
    stop_speaking_flag = True

def process_command(query):
    if not query or query == "none": return
    safe_update_chat(f"👤 You: {query}")
    log_interaction("user", query)
    
    if 'time' in query: 
        threading.Thread(target=speak, args=(f"The time is {datetime.datetime.now().strftime('%H:%M:%S')}",), daemon=True).start()
    elif 'exit' in query or 'bye' in query: 
        threading.Thread(target=speak, args=("Goodbye!",), daemon=True).start()
        root.after(2000, root.quit)
    else: 
        threading.Thread(target=stream_ai_response, args=(query,), daemon=True).start()

def handle_text_input(event=None):
    q = text_input.get().strip().lower()
    text_input.delete(0, tk.END)
    if q: 
        interrupt_ai() # Stop speaking when sending a new command
        process_command(q)

# --- Push To Talk Logic ---
def start_recording(event):
    global is_recording, audio_frames, pyaudio_instance, audio_stream
    if is_recording: return
    
    interrupt_ai() # Immediately stop AI speech when pressing the mic button
    
    is_recording = True
    audio_frames = []
    
    mic_btn.config(bg="#E53935", text="🎙️ Recording...")
    safe_update_chat("System: Listening... (Release to stop)")
    
    pyaudio_instance = pyaudio.PyAudio()
    audio_stream = pyaudio_instance.open(format=pyaudio.paInt16, channels=1, rate=44100, input=True, frames_per_buffer=1024)
    
    def record_loop():
        while is_recording:
            audio_frames.append(audio_stream.read(1024, exception_on_overflow=False))
            
    threading.Thread(target=record_loop, daemon=True).start()

def stop_recording(event):
    global is_recording, audio_frames, pyaudio_instance, audio_stream
    if not is_recording: return
    is_recording = False
    
    mic_btn.config(bg="#007ACC", text="🎤 Hold to Talk")
    
    audio_stream.stop_stream()
    audio_stream.close()
    pyaudio_instance.terminate()
    
    wf = wave.open("temp_voice.wav", 'wb')
    wf.setnchannels(1)
    wf.setsampwidth(pyaudio_instance.get_sample_size(pyaudio.paInt16))
    wf.setframerate(44100)
    wf.writeframes(b''.join(audio_frames))
    wf.close()
    
    def transcribe():
        safe_update_chat("System: Transcribing...")
        r = sr.Recognizer()
        with sr.AudioFile("temp_voice.wav") as source:
            audio_data = r.record(source)
        try:
            query = r.recognize_google(audio_data, language='en-in').lower()
            process_command(query)
        except:
            safe_update_chat("System: No speech detected.")
        finally:
            if os.path.exists("temp_voice.wav"): os.remove("temp_voice.wav")
            
    threading.Thread(target=transcribe, daemon=True).start()

# --- GUI Construction ---
init_db()

BG_COLOR = "#1E1E1E"
BOX_COLOR = "#252526"
INPUT_BG = "#3C3C3C"
TEXT_COLOR = "#D4D4D4"
BTN_COLOR = "#007ACC"

root = tk.Tk()
root.title("Agentic AI Assistant")
root.geometry("600x750")
root.configure(bg=BG_COLOR)

chat_font = font.Font(family="Consolas", size=11)
ui_font = font.Font(family="Segoe UI", size=12)

chat_box = scrolledtext.ScrolledText(root, wrap=tk.WORD, state=tk.DISABLED, font=chat_font, bg=BOX_COLOR, fg=TEXT_COLOR, insertbackground=TEXT_COLOR, relief=tk.FLAT, padx=15, pady=15)
chat_box.pack(padx=20, pady=20, fill=tk.BOTH, expand=True)

input_frame = tk.Frame(root, bg=BG_COLOR)
input_frame.pack(padx=20, pady=(0, 20), fill=tk.X)

text_input = tk.Entry(input_frame, font=ui_font, bg=INPUT_BG, fg=TEXT_COLOR, insertbackground=TEXT_COLOR, relief=tk.FLAT)
text_input.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=8, padx=(0, 10))

# Bind keystrokes in the text box to instantly interrupt the AI
text_input.bind("<KeyPress>", interrupt_ai)
text_input.bind("<Return>", handle_text_input)

send_btn = tk.Button(input_frame, text="Send", command=handle_text_input, bg=BTN_COLOR, fg="white", font=ui_font, relief=tk.FLAT, cursor="hand2", width=8)
send_btn.pack(side=tk.LEFT, padx=(0, 10), ipady=4)

mic_btn = tk.Button(input_frame, text="🎤 Hold to Talk", bg=BTN_COLOR, fg="white", font=ui_font, relief=tk.FLAT, cursor="hand2", width=14)
mic_btn.pack(side=tk.LEFT, ipady=4)

mic_btn.bind("<ButtonPress-1>", start_recording)
mic_btn.bind("<ButtonRelease-1>", stop_recording)

root.mainloop()