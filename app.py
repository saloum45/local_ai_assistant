import sys
import os
import time
import json
import queue
import subprocess
import threading
import platform
import webbrowser
import pyttsx3
import ollama
import speech_recognition as sr
from PyQt6.QtCore import Qt, QUrl, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMainWindow
from PyQt6.QtWebEngineWidgets import QWebEngineView

# Configuration
MODEL_NAME = "qwen2.5:1.5b"
WINDOW_W, WINDOW_H = 270, 160   # l'avatar fait 260x150 ; le reste laisse la place au léger flottement
WINDOW_MARGIN = 12              # distance au bord de l'écran
HTML_FILE = "index.html"  # le fichier de l'avatar, dans le même dossier que ce script
SYSTEM_PROMPT = (
    "Tu es un assistant virtuel vivant sur le bureau de ton utilisateur. "
    "Tes réponses doivent être très courtes, directes et en français. "
    "Si l'utilisateur te demande d'effectuer une action sur sa machine (ouvrir une application, le navigateur, regarder l'heure, etc.), "
    "utilise l'outil approprié mis à ta disposition."
)


TTS_SCRIPT = r"""
import sys, json, pyttsx3
engine = pyttsx3.init()                      # démarrage lourd fait à l'avance
engine.setProperty("rate", 165)
for v in engine.getProperty("voices"):
    langs = " ".join(str(l) for l in (getattr(v, "languages", None) or []))
    h = f"{v.id} {v.name} {langs}".lower()
    if ("french" in h or "fr-fr" in h or "fr_fr" in h or "fr-be" in h
            or "fr-ca" in h or "fr-ch" in h or v.id.lower().endswith(("/fr", "\\fr"))):
        engine.setProperty("voice", v.id)
        break
engine.connect("started-utterance", lambda name: print("START", flush=True))
text = json.loads(sys.stdin.readline() or '""')   # attend le texte
if text:
    engine.say(text)
    engine.runAndWait()
"""


# ==========================================
# DÉFINITION DES OUTILS (FONCTIONS SYSTÈME)
# ==========================================

def open_browser(url="https://www.google.com"):
    """Ouvre le navigateur web par défaut."""
    try:
        webbrowser.open(url)
        return "Navigateur ouvert avec succès."
    except Exception as e:
        return f"Erreur lors de l'ouverture du navigateur : {e}"

def open_terminal():
    """Ouvre le terminal du système."""
    try:
        system = platform.system()
        if system == "Darwin":  # macOS
            subprocess.Popen(["open", "-a", "Terminal"])
        elif system == "Windows":
            subprocess.Popen(["cmd.exe"])
        else:  # Linux
            subprocess.Popen(["gnome-terminal"])
        return "Terminal ouvert."
    except Exception as e:
        return f"Erreur lors de l'ouverture du terminal : {e}"

def get_current_time():
    """Renvoie l'heure actuelle."""
    current_time = time.strftime("%H:%M")
    return f"Il est actuellement {current_time}."

def list_desktop():
    """Liste les fichiers présents sur le bureau."""
    try:
        desktop_path = os.path.expanduser("~/Desktop")
        files = os.listdir(desktop_path)
        # Filtrer les fichiers cachés
        visible_files = [f for f in files if not f.startswith('.')]
        return f"Voici quelques fichiers sur ton bureau : {', '.join(visible_files[:5])}."
    except Exception as e:
        return f"Impossible de lire le bureau : {e}"

# Registre des outils disponibles pour l'appel de fonctions Ollama
AVAILABLE_TOOLS = {
    "open_browser": open_browser,
    "open_terminal": open_terminal,
    "get_current_time": get_current_time,
    "list_desktop": list_desktop,
}

# Déclarations des outils au format Ollama / OpenAI
OLLAMA_TOOLS_DEFINITIONS = [
    {
        'type': 'function',
        'function': {
            'name': 'open_browser',
            'description': 'Ouvre le navigateur web par défaut de l\'ordinateur.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'url': {'type': 'string', 'description': 'URL optionnelle à ouvrir (par défaut Google)'}
                },
                'required': []
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'open_terminal',
            'description': 'Ouvre une fenêtre de terminal sur le bureau.',
            'parameters': {'type': 'object', 'properties': {}, 'required': []}
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_current_time',
            'description': 'Donne l\'heure actuelle exacte.',
            'parameters': {'type': 'object', 'properties': {}, 'required': []}
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_desktop',
            'description': 'Liste les fichiers présents sur le bureau de l\'utilisateur.',
            'parameters': {'type': 'object', 'properties': {}, 'required': []}
        }
    }
]


class AssistantBridge(QObject):
    speak_signal = pyqtSignal(bool)
    listen_signal = pyqtSignal(bool)
    think_signal = pyqtSignal(bool)


class AssistantApp(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # macOS : sans ça, une fenêtre de type Tool disparaît quand l'app perd le focus
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.resize(WINDOW_W, WINDOW_H)
        self.move_to_top_right()

        # --- Avatar (WebView) ---
        self.page_ready = False
        self.browser = QWebEngineView(self)
        self.browser.page().setBackgroundColor(Qt.GlobalColor.transparent)
        self.browser.loadFinished.connect(self.on_page_loaded)

        # Chemin relatif au script, pas au dossier d'où tu lances Python
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), HTML_FILE)
        if not os.path.exists(html_path):
            print(f"[ERREUR] Fichier introuvable : {html_path}")
        self.browser.load(QUrl.fromLocalFile(html_path))
        self.setCentralWidget(self.browser)

        # --- Pont d'événements (thread -> interface) ---
        self.bridge = AssistantBridge()
        self.bridge.speak_signal.connect(lambda s: self.call_js("setSpeaking", s))
        self.bridge.listen_signal.connect(lambda s: self.call_js("setListening", s))
        self.bridge.think_signal.connect(lambda s: self.call_js("setThinking", s))

        # --- Voix : un thread dédié qui possède son propre moteur ---
        self.tts_queue = queue.Queue()
        self.speaking = threading.Event()   # actif de l'envoi du texte jusqu'à la fin de la parole
        threading.Thread(target=self.tts_worker, daemon=True).start()

        # --- Micro ---
        self.recognizer = sr.Recognizer()
        self.microphone = sr.Microphone()
        with self.microphone as source:
            self.recognizer.adjust_for_ambient_noise(source)

        # --- Démarrage ---
        threading.Thread(target=self.run_assistant_task,
                         args=("Dis bonjour en 1 phrase courte.",), daemon=True).start()
        self.is_listening_loop = True
        threading.Thread(target=self.listen_loop, daemon=True).start()

    # ---------- Interface ----------
    def move_to_top_right(self):
        """Coin haut-droit de l'écran, sous la barre de menus du Mac."""
        area = QApplication.primaryScreen().availableGeometry()
        x = area.x() + area.width() - self.width() - WINDOW_MARGIN
        y = area.y() + WINDOW_MARGIN
        self.move(x, y)

    def on_page_loaded(self, ok):
        self.page_ready = ok
        print("Avatar chargé." if ok else "[ERREUR] La page de l'avatar n'a pas pu se charger.")

    def call_js(self, func, state):
        """Appelle une fonction JS de l'avatar seulement si la page est prête et la fonction existe."""
        if not self.page_ready:
            return
        value = str(bool(state)).lower()
        self.browser.page().runJavaScript(
            f"if (typeof {func} === 'function') {func}({value});"
        )

    # ---------- Voix ----------
    def speak(self, text):
        """Peut être appelée depuis n'importe quel thread."""
        self.speaking.set()          # bloque l'écoute tout de suite
        self.tts_queue.put(text)

    def spawn_tts(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.Popen(
            [sys.executable, "-c", TTS_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=flags
        )

    def tts_worker(self):
        """Un processus de voix est toujours préchauffé (imports + moteur prêts).
        Il dit UNE phrase puis se termine, et le suivant est déjà lancé : pas de délai,
        et pas de blocage de pyttsx3 sur la 2e phrase."""
        proc = self.spawn_tts()
        while True:
            text = self.tts_queue.get()
            mouth_open = False
            other = []
            try:
                if proc.poll() is not None:        # le processus préchauffé est mort : on en relance un
                    proc = self.spawn_tts()
                proc.stdin.write(json.dumps(text) + "\n")
                proc.stdin.flush()
                proc.stdin.close()
                next_proc = self.spawn_tts()       # préchauffe le suivant pendant que celui-ci parle
                for line in proc.stdout:
                    if line.strip() == "START":
                        mouth_open = True
                        self.bridge.speak_signal.emit(True)
                    elif line.strip():
                        other.append(line.strip())
                proc.wait(timeout=60)
                if proc.returncode != 0 or (not mouth_open and other):
                    print("Erreur TTS: " + " | ".join(other))
            except Exception as e:
                print(f"Erreur TTS: {e}")
                next_proc = self.spawn_tts()
            finally:
                self.bridge.speak_signal.emit(False)
                self.speaking.clear()
            proc = next_proc

    # ---------- IA ----------
    def run_assistant_task(self, prompt):
        try:
            self.bridge.listen_signal.emit(False)
            self.bridge.think_signal.emit(True)

            messages = [
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt}
            ]

            # Premier appel à Ollama avec transmission des outils disponibles
            response = ollama.chat(
                model=MODEL_NAME,
                messages=messages,
                tools=OLLAMA_TOOLS_DEFINITIONS
            )

            # Vérifie si le modèle souhaite exécuter une fonction (tool call)
            if response.get('message', {}).get('tool_calls'):
                messages.append(response['message'])
                
                for tool in response['message']['tool_calls']:
                    fn_name = tool['function']['name']
                    fn_args = tool['function'].get('arguments', {})
                    
                    print(f"Exécution de la tâche système : {fn_name} avec les args {fn_args}")
                    
                    if fn_name in AVAILABLE_TOOLS:
                        # Exécute la fonction Python correspondante
                        result = AVAILABLE_TOOLS[fn_name](**fn_args)
                    else:
                        result = f"Erreur : fonction {fn_name} inconnue."

                    # Ajoute le résultat de l'outil dans l'historique de conversation
                    messages.append({
                        'role': 'tool',
                        'content': str(result),
                    })

                # Second appel à Ollama pour qu'il formule une réponse textuelle à partir du résultat
                response = ollama.chat(
                    model=MODEL_NAME,
                    messages=messages
                )

            reply = response['message']['content']
            print(f"Assistant: {reply}")
            self.speak(reply)
        except Exception as e:
            print(f"Erreur Ollama: {e}")
        finally:
            self.bridge.think_signal.emit(False)

    # ---------- Écoute ----------
    def listen_loop(self):
        while self.is_listening_loop:
            # Ne pas s'écouter soi-même pendant que l'assistant parle
            while self.speaking.is_set():
                time.sleep(0.1)
            try:
                with self.microphone as source:
                    self.bridge.listen_signal.emit(True)
                    print("J'écoute...")
                    audio = self.recognizer.listen(source, timeout=5, phrase_time_limit=10)
                    self.bridge.listen_signal.emit(False)

                text = self.recognizer.recognize_google(audio, language="fr-FR")
                print(f"Utilisateur (voix) : {text}")
                self.run_assistant_task(text)

            except sr.WaitTimeoutError:
                pass
            except sr.UnknownValueError:
                pass
            except Exception as e:
                print(f"Erreur audio: {e}")
            finally:
                self.bridge.listen_signal.emit(False)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = AssistantApp()
    window.show()
    sys.exit(app.exec())