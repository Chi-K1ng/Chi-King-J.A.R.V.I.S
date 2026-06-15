# J.A.R.V.I.S. Live

A real-time voice AI assistant powered by the **Gemini Live API** with a futuristic HUD interface. Speak to it, and it speaks back — with tool use, live web search, weather, system control, file access, and more.

---

## Features

- **Real-time voice conversation** — Gemini 2.5 Flash Native Audio (low-latency two-way audio)
- **Barge-in / interruption** — talk over JARVIS and it stops immediately
- **30 voice options** — switch voices live mid-session without disconnecting
- **Live web search** — asks DuckDuckGo for current prices, news, scores, and facts
- **Weather** — current conditions + 7-day forecast for any city (Open-Meteo, no API key needed)
- **System monitoring** — CPU, RAM, GPU, disk, top processes, uptime
- **File system tools** — read, write, move, search files via voice
- **System control** — launch/close apps, keyboard shortcuts, volume, clipboard, power control
- **Browser control** — open URLs and Gmail via voice
- **Telegram integration** (optional) — chat with JARVIS over Telegram with full tool access
- **Futuristic HUD** — animated canvas core, waveform meters, chat transcript, telemetry panels
- **Auto-reconnect** — recovers from dropped Gemini sessions automatically

---

## Requirements

- Python 3.11+
- Windows (uses WMI and Windows audio APIs for system monitoring)
- A [Google Gemini API key](https://aistudio.google.com/apikey) with Live API access
- A microphone

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/your-username/jarvis-live.git
cd jarvis-live
```

### 2. Create a virtual environment

```bash
python -m venv venv
venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

> **Note:** `pycaw` is optional — it enables precise volume control. Without it, volume commands fall back to key events.
> `python-telegram-bot` is optional — only needed if you want the Telegram integration.

### 4. Configure your environment

Copy the example env file and fill it in:

```bash
copy .env.example .env
```

Open `.env` and set at minimum:

```
GEMINI_API_KEY=your_key_here
JARVIS_OWNER_NAME=YourName
```

See `.env.example` for all available options.

### 5. Run

```bash
venv\Scripts\python app.py
```

Then open **http://localhost:8080** in your browser, click **INITIALIZE**, and start talking.

---

## Personalisation (.env options)

| Variable | Description | Default |
|---|---|---|
| `GEMINI_API_KEY` | Your Gemini API key **(required)** | — |
| `JARVIS_OWNER_NAME` | Your name — JARVIS will address you by this | `Boss` |
| `JARVIS_CPU` | Describe your CPU for JARVIS to reference | `a high-performance CPU` |
| `JARVIS_RAM` | Describe your RAM | `high-capacity RAM` |
| `JARVIS_GPU` | Describe your GPU | `a dedicated GPU` |
| `JARVIS_STORAGE` | Describe your storage | `fast NVMe SSD storage` |
| `JARVIS_OS` | Your OS | `Windows` |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (optional) | — |
| `TELEGRAM_ALLOWED_USER_ID` | Your Telegram user ID to restrict access (optional) | — |

---

## Project Structure

```
jarvis-live/
├── app.py                  ← Flask server + Gemini Live proxy + all tools
├── templates/
│   └── index.html          ← HUD layout
├── static/
│   ├── css/style.css       ← Dark cyan sci-fi theme
│   └── js/app.js           ← AudioWorklet, WebSocket, canvas animation
├── .env.example            ← Environment variable template
├── requirements.txt        ← Python dependencies
└── README.md
```

---

## Tech Stack

| Layer | Tech |
|---|---|
| Server | Python, Flask, flask-sock (WebSocket) |
| AI | Google Gemini Live API — `gemini-2.5-flash-native-audio-latest` |
| SDK | `google-genai >= 2.8` |
| Web search | DuckDuckGo Instant Answer API (free, no key) |
| Weather | Open-Meteo API (free, no key) |
| System monitoring | `psutil`, `wmi`, `nvidia-smi` |
| Frontend | Vanilla JS + Canvas HUD, Web Audio API |
| Mic input | 16 kHz PCM via AudioWorklet |
| Speaker output | 24 kHz PCM via AudioBufferSourceNode queue |

---

## Notes

- **GPU temperature** requires [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) running in the background on Windows. Without it, JARVIS will note that CPU temp is unavailable.
- The app binds to `0.0.0.0:8080` so you can access it from your phone on the same network.
- For best results use Chrome or Edge — Safari has inconsistent AudioWorklet support.
