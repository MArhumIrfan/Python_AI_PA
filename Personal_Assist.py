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
import time
from dotenv import load_dotenv
from google import genai

class DatabaseManager:
    """Handles all SQLite operations using Python context managers."""
    
    def __init__(self, db_name="assistant_memory.db"):
        self.db_name = db_name
        self.init_db()

    def init_db(self):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS chat_history 
                            (id INTEGER PRIMARY KEY, role TEXT, message TEXT)''')

    def log_interaction(self, role, message):
        with sqlite3.connect(self.db_name) as conn:
            conn.execute("INSERT INTO chat_history (role, message) VALUES (?, ?)", (role, message))

    def get_recent_context(self, limit=4, max_chars=500):
        with sqlite3.connect(self.db_name) as conn:
            rows = conn.execute("SELECT role, message FROM chat_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        
        context_string = ""
        for role, msg in reversed(rows):
            if len(msg) > max_chars:
                msg = msg[:max_chars] + "... [TRUNCATED TO SAVE TOKENS]"
            context_string += f"{role.upper()}: {msg}\n"
        return context_string


class AssistantApp:
    """Main Application Class handling the GUI, Audio, and AI Logic."""
    
    def __init__(self, root):
        self.root = root
        self.db = DatabaseManager()
        
        # --- Environment & API Setup ---
        load_dotenv()
        self.client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        self.user_profile = os.environ.get('USERPROFILE', 'C:\\')
        
        # --- Encapsulated State Variables ---
        self.is_recording = False
        self.audio_frames = []
        self.pyaudio_instance = None
        self.audio_stream = None
        self.stop_speaking_flag = False
        
        self.setup_ui()

    def setup_ui(self):
        """Constructs the modern Dark Theme Tkinter interface."""
        self.root.title("Agentic AI Assistant")
        self.root.geometry("600x750")
        
        BG_COLOR, BOX_COLOR, INPUT_BG = "#1E1E1E", "#252526", "#3C3C3C"
        TEXT_COLOR, BTN_COLOR = "#D4D4D4", "#007ACC"
        self.root.configure(bg=BG_COLOR)

        chat_font = font.Font(family="Consolas", size=11)
        ui_font = font.Font(family="Segoe UI", size=12)

        self.chat_box = scrolledtext.ScrolledText(self.root, wrap=tk.WORD, state=tk.DISABLED, font=chat_font, 
                                                  bg=BOX_COLOR, fg=TEXT_COLOR, insertbackground=TEXT_COLOR, 
                                                  relief=tk.FLAT, padx=15, pady=15)
        self.chat_box.pack(padx=20, pady=20, fill=tk.BOTH, expand=True)

        input_frame = tk.Frame(self.root, bg=BG_COLOR)
        input_frame.pack(padx=20, pady=(0, 20), fill=tk.X)

        self.text_input = tk.Entry(input_frame, font=ui_font, bg=INPUT_BG, fg=TEXT_COLOR, 
                                   insertbackground=TEXT_COLOR, relief=tk.FLAT)
        self.text_input.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=8, padx=(0, 10))
        self.text_input.bind("<KeyPress>", self.interrupt_ai)
        self.text_input.bind("<Return>", self.handle_text_input)

        self.send_btn = tk.Button(input_frame, text="Send", command=self.handle_text_input, bg=BTN_COLOR, 
                                  fg="white", font=ui_font, relief=tk.FLAT, cursor="hand2", width=8)
        self.send_btn.pack(side=tk.LEFT, padx=(0, 10), ipady=4)

        self.mic_btn = tk.Button(input_frame, text="🎤 Hold to Talk", bg=BTN_COLOR, fg="white", 
                                 font=ui_font, relief=tk.FLAT, cursor="hand2", width=14)
        self.mic_btn.pack(side=tk.LEFT, ipady=4)
        
        self.mic_btn.bind("<ButtonPress-1>", self.start_recording)
        self.mic_btn.bind("<ButtonRelease-1>", self.stop_recording)

    # --- Thread-Safe GUI Updaters ---
    def _insert_text(self, text):
        self.chat_box.config(state=tk.NORMAL)
        self.chat_box.insert(tk.END, text)
        self.chat_box.config(state=tk.DISABLED)
        self.chat_box.yview(tk.END)

    def safe_update_chat(self, msg, newline=True):
        formatted_msg = msg + ("\n\n" if newline else "")
        self.root.after(0, self._insert_text, formatted_msg)

    # --- Push To Talk Logic ---
    def start_recording(self, event=None):
        if self.is_recording: return
        self.interrupt_ai()
        
        self.is_recording = True
        self.audio_frames = []
        self.mic_btn.config(bg="#E53935", text="🎙️ Recording...")
        self.safe_update_chat("System: Listening... (Release to stop)")
        
        self.pyaudio_instance = pyaudio.PyAudio()
        self.audio_stream = self.pyaudio_instance.open(format=pyaudio.paInt16, channels=1, 
                                                       rate=44100, input=True, frames_per_buffer=1024)
        threading.Thread(target=self._record_loop, daemon=True).start()

    def _record_loop(self):
        while self.is_recording:
            self.audio_frames.append(self.audio_stream.read(1024, exception_on_overflow=False))

    def stop_recording(self, event=None):
        if not self.is_recording: return
        self.is_recording = False
        
        self.mic_btn.config(bg="#007ACC", text="🎤 Hold to Talk")
        
        self.audio_stream.stop_stream()
        self.audio_stream.close()
        self.pyaudio_instance.terminate()
        
        with wave.open("temp_voice.wav", 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(self.pyaudio_instance.get_sample_size(pyaudio.paInt16))
            wf.setframerate(44100)
            wf.writeframes(b''.join(self.audio_frames))
            
        threading.Thread(target=self.transcribe_and_process, daemon=True).start()

    def transcribe_and_process(self):
        self.safe_update_chat("System: Transcribing...")
        r = sr.Recognizer()
        try:
            with sr.AudioFile("temp_voice.wav") as source:
                audio_data = r.record(source)
            query = r.recognize_google(audio_data, language='en-in').lower()
            self.process_command(query)
        except sr.UnknownValueError:
            self.safe_update_chat("System: No speech detected.")
        except Exception as e:
            self.safe_update_chat(f"System: Error transcribing - {e}")
        finally:
            if os.path.exists("temp_voice.wav"): os.remove("temp_voice.wav")

    # --- Intelligent Speaking Logic ---
    def interrupt_ai(self, event=None):
        self.stop_speaking_flag = True

    def speak(self, text):
        self.stop_speaking_flag = False
        
        clean_text = re.sub(r'```.*?```', 'the requested code', text, flags=re.DOTALL)
        clean_text = re.sub(r'\[(?:FILE|CMD).*?\]', '', clean_text)
        clean_text = re.sub(r'[*#_~`]', '', clean_text) 
        
        sentences = [s.strip() for s in re.split(r'(?<=[.!?]) +', clean_text) if s.strip()]
        
        try:
            engine = pyttsx3.init()
            for sentence in sentences:
                if self.stop_speaking_flag: break
                engine.say(sentence)
                engine.runAndWait()
        except: pass

    # --- System Command Parser ---
    def execute_system_commands(self, text):
        feedback_logs = []

        # Single argument commands
        for cmd, arg in re.findall(r'\[CMD:\s*([A-Z_]+),\s*(?:path|url):\s*"(.*?)"\]', text):
            try:
                if cmd == "MKDIR": os.makedirs(arg, exist_ok=True); feedback_logs.append(f"SUCCESS: Created folder '{arg}'")
                elif cmd == "DELETE":
                    if os.path.isfile(arg): os.remove(arg)
                    elif os.path.isdir(arg): shutil.rmtree(arg)
                    feedback_logs.append(f"SUCCESS: Deleted '{arg}'")
                elif cmd == "OPEN_LINK": webbrowser.open(arg); feedback_logs.append(f"SUCCESS: Opened URL '{arg}'")
                elif cmd == "OPEN_MEDIA": os.startfile(arg); feedback_logs.append(f"SUCCESS: Opened file '{arg}'")
                elif cmd == "LIST_DIR":
                    items = os.listdir(arg)
                    items_str = ", ".join(items[:50]) + (f" ... (+{len(items)-50} more)" if len(items) > 50 else "")
                    feedback_logs.append(f"SUCCESS: Contents of '{arg}': {items_str}")
            except Exception as e: feedback_logs.append(f"ERROR ({cmd} on {arg}): {e}")

        # Double argument commands
        for cmd, src, dst in re.findall(r'\[CMD:\s*([A-Z_]+),\s*src:\s*"(.*?)",\s*dst:\s*"(.*?)"\]', text):
            try:
                if cmd == "MOVE": shutil.move(src, dst); feedback_logs.append(f"SUCCESS: Moved '{src}' to '{dst}'")
                elif cmd == "COPY":
                    if os.path.isdir(src): shutil.copytree(src, dst)
                    else: shutil.copy2(src, dst)
                    feedback_logs.append(f"SUCCESS: Copied '{src}' to '{dst}'")
            except Exception as e: feedback_logs.append(f"ERROR ({cmd}): {e}")

        # WhatsApp automation
        for phone, msg in re.findall(r'\[CMD:\s*WHATSAPP,\s*phone:\s*"(.*?)",\s*message:\s*"(.*?)"\]', text):
            try:
                webbrowser.open(f"whatsapp://send?phone={phone}&text={urllib.parse.quote(msg)}")
                feedback_logs.append(f"SUCCESS: Opened WhatsApp for {phone}")
            except Exception as e: feedback_logs.append(f"ERROR (WHATSAPP): {e}")

        # File generation
        match_name = re.search(r'\[FILE:\s*(.*?)\]', text)
        match_code = re.search(r'```(?:[a-zA-Z0-9\+]*\n)?(.*?)```', text, re.DOTALL)
        if match_name and match_code:
            filename, code_content = match_name.group(1).strip(), match_code.group(1).strip()
            try:
                with open(filename, 'w', encoding='utf-8') as f: f.write(code_content)
                feedback_logs.append(f"SUCCESS: Saved file '{filename}'")
            except Exception as e: feedback_logs.append(f"ERROR (FILE): {e}")

        for feedback in feedback_logs:
            self.safe_update_chat(f"⚙️ {feedback}")
            self.db.log_interaction("system", feedback)

    # --- Core AI Logic ---
    def process_command(self, query):
        if not query or query == "none": return
        self.safe_update_chat(f"👤 You: {query}")
        self.db.log_interaction("user", query)
        
        if 'time' in query: 
            threading.Thread(target=self.speak, args=(f"The time is {datetime.datetime.now().strftime('%H:%M:%S')}",), daemon=True).start()
        elif 'exit' in query or 'bye' in query: 
            threading.Thread(target=self.speak, args=("Goodbye!",), daemon=True).start()
            self.root.after(2000, self.root.quit)
        else: 
            threading.Thread(target=self.stream_ai_response, args=(query,), daemon=True).start()

    def handle_text_input(self, event=None):
        q = self.text_input.get().strip().lower()
        self.text_input.delete(0, tk.END)
        if q: 
            self.interrupt_ai()
            self.process_command(q)

    def stream_ai_response(self, prompt):
        sys_instruct = (
            f"You are an agentic OS assistant running on Windows 11.\n"
            f"User's home directory: {self.user_profile}\n"
            f"Use double Windows backslashes (\\\\) for paths.\n\n"
            "Tags:\n"
            "- [CMD: MKDIR, path: \"...\"]\n"
            "- [CMD: DELETE, path: \"...\"]\n"
            "- [CMD: MOVE, src: \"...\", dst: \"...\"]\n"
            "- [CMD: COPY, src: \"...\", dst: \"...\"]\n"
            "- [CMD: OPEN_LINK, url: \"https...\"]\n"
            "- [CMD: OPEN_MEDIA, path: \"...\"]\n"
            "- [CMD: WHATSAPP, phone: \"+1...\", message: \"...\"]\n"
            "- [CMD: LIST_DIR, path: \"...\"]\n"
            "- [FILE: path\\\\name.ext] + ```code```\n\n"
            "CRITICAL RULES:\n"
            "1. NEVER use the [CMD: ...] or [FILE: ...] tags as examples in your text. ONLY output them to execute the action.\n"
            "2. If asked to open a file without the exact name, use LIST_DIR to scan the folder first, then use OPEN_MEDIA.\n"
            "3. MEMORY USAGE: The conversation history is provided for background context ONLY. Treat every new prompt as a fresh interaction. DO NOT mention or resume previous topics unless the user explicitly refers back to them."
        )
        full_prompt = f"{sys_instruct}\n\nHistory:\n{self.db.get_recent_context()}\nUSER: {prompt}"
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                chat = self.client.chats.create(model="gemini-3.6-flash")
                response_stream = chat.send_message_stream(full_prompt)
                
                self.safe_update_chat("🤖 Assistant: ", newline=False)
                full_response = ""
                
                for chunk in response_stream:
                    full_response += chunk.text
                    ui_text = re.sub(r'^\s*[\*\-]\s+', '➔ ', chunk.text, flags=re.MULTILINE)
                    self.root.after(0, self._insert_text, ui_text)
                    
                self.safe_update_chat("") 
                self.db.log_interaction("assistant", full_response)
                self.execute_system_commands(full_response)
                
                threading.Thread(target=self.speak, args=(full_response,), daemon=True).start()
                break 
                
            except Exception as e:
                error_message = str(e)
                if "503" in error_message and attempt < max_retries - 1:
                    self.safe_update_chat(f"⚙️ System: Servers are busy. Retrying... (Attempt {attempt + 2}/{max_retries})")
                    time.sleep(2) 
                elif "429" in error_message:
                    wait_time_match = re.search(r'retry in (\d+)s', error_message)
                    wait_seconds = wait_time_match.group(1) if wait_time_match else "60"
                    self.safe_update_chat(f"⚠️ System: Free tier API limit reached! Please wait {wait_seconds} seconds before sending another command.")
                    break 
                else:
                    self.safe_update_chat(f"\n⚠️ [AI Error]: {e}")
                    break 


if __name__ == "__main__":
    root = tk.Tk()
    app = AssistantApp(root)
    root.mainloop()