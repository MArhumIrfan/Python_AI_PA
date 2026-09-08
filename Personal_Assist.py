import tkinter as tk
from tkinter import scrolledtext
import threading
import speech_recognition as sr
import pyttsx3
import datetime
import wikipedia
import webbrowser
import pyjokes
import sqlite3
from google import genai

# Setup your AI Brain
# Replace 'YOUR_API_KEY_HERE' with your actual key
client = genai.Client(api_key="###")

# --- Database Management ---
def init_db():
    """Creates the relational database schema if it doesn't exist."""
    conn = sqlite3.connect("assistant_memory.db")
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def log_interaction(role, message):
    """Inserts a new record into the database."""
    conn = sqlite3.connect("assistant_memory.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO chat_history (role, message) VALUES (?, ?)", (role, message))
    conn.commit()
    conn.close()

def get_recent_context(limit=6):
    """Retrieves the last few interactions to give the AI memory."""
    conn = sqlite3.connect("assistant_memory.db")
    cursor = conn.cursor()
    cursor.execute("SELECT role, message FROM chat_history ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    conn.close()
    
    # The database returns newest first, so we reverse it to chronological order
    context_string = ""
    for role, msg in reversed(rows):
        context_string += f"{role.upper()}: {msg}\n"
    return context_string

# --- Core Assistant Logic ---
def ask_ai(prompt):
    """Sends the context and query to the model."""
    try:
        # Build the prompt using the database records
        past_context = get_recent_context()
        full_prompt = (
            "Here is the recent conversation history for context:\n"
            f"{past_context}\n"
            "Please respond to the USER's latest message naturally."
        )
        
        chat = client.chats.create(model="gemini-3.6-flash")
        response = chat.send_message(full_prompt)
        
        # Save the AI's answer to the database
        log_interaction("assistant", response.text)
        return response.text
    except Exception as e:
        return f"I am having trouble connecting to my AI brain. Error: {e}"

def update_chat(msg):
    """Inserts a new message into the GUI chat window."""
    chat_box.config(state=tk.NORMAL)
    chat_box.insert(tk.END, msg + "\n\n")
    chat_box.config(state=tk.DISABLED)
    chat_box.yview(tk.END)

def speak(text, is_ai_response=False):
    """Prints to the GUI, logs it if it's an AI response, and speaks out loud."""
    update_chat(f"Assistant: {text}")
    
    # We only log hardcoded responses here; dynamic AI responses are logged in ask_ai()
    if not is_ai_response:
        log_interaction("assistant", text)
        
    try:
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
    except:
        pass

def process_command(query):
    """Handles the core logic of the assistant in a background thread."""
    if query == "none" or not query:
        return

    update_chat(f"You: {query}")
    log_interaction("user", query)

    if 'wikipedia' in query:
        speak("Searching Wikipedia...")
        query = query.replace("wikipedia", "")
        try:
            result = wikipedia.summary(query, sentences=2)
            speak(f"According to Wikipedia:\n{result}")
        except:
            speak("Sorry, I couldn't find anything.")

    elif 'open youtube' in query:
        speak("Opening YouTube...")
        webbrowser.open("https://www.youtube.com/")

    elif 'time' in query:
        strTime = datetime.datetime.now().strftime("%H:%M:%S")
        speak(f"The current time is {strTime}")

    elif 'exit' in query or 'bye' in query or 'quit' in query:
        speak("Goodbye! Have a nice day!")
        root.quit()

    else:
        speak("Let me think about that...", is_ai_response=True)
        ai_response = ask_ai(query)
        speak(ai_response, is_ai_response=True)

def handle_text_input(event=None):
    """Captures typed text and sends it to the processor."""
    query = text_input.get().strip().lower()
    text_input.delete(0, tk.END)
    if query:
        threading.Thread(target=process_command, args=(query,), daemon=True).start()

def handle_voice_input():
    """Listens to the microphone in a background thread."""
    def listen_and_process():
        r = sr.Recognizer()
        with sr.Microphone() as source:
            update_chat("System: Listening (Speak now)...")
            r.adjust_for_ambient_noise(source, duration=0.5)
            try:
                audio = r.listen(source, timeout=5, phrase_time_limit=10)
                update_chat("System: Recognizing...")
                query = r.recognize_google(audio, language='en-in').lower()
                process_command(query)
            except sr.WaitTimeoutError:
                update_chat("System: No speech detected.")
            except Exception:
                update_chat("System: Say that again please...")
                
    threading.Thread(target=listen_and_process, daemon=True).start()

# --- GUI Construction ---
init_db() # Run the database setup immediately

root = tk.Tk()
root.title("Personal AI Assistant")
root.geometry("500x650")
root.configure(bg="#f0f0f0")

chat_box = scrolledtext.ScrolledText(root, wrap=tk.WORD, state=tk.DISABLED, font=("Consolas", 11))
chat_box.pack(padx=10, pady=10, fill=tk.BOTH, expand=True)

input_frame = tk.Frame(root, bg="#f0f0f0")
input_frame.pack(padx=10, pady=10, fill=tk.X)

text_input = tk.Entry(input_frame, font=("Arial", 12))
text_input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
text_input.bind("<Return>", handle_text_input)

send_btn = tk.Button(input_frame, text="Send", width=8, command=handle_text_input, bg="#4CAF50", fg="white", font=("Arial", 10, "bold"))
send_btn.pack(side=tk.LEFT)

mic_btn = tk.Button(input_frame, text="🎤 Mic", width=8, command=handle_voice_input, bg="#2196F3", fg="white", font=("Arial", 10, "bold"))
mic_btn.pack(side=tk.LEFT, padx=(10, 0))

root.mainloop()
