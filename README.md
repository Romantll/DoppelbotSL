# DoppelbotSL

A multiplayer social deduction game built for research on human-AI interaction. Human players and AI bots share a chat room and players have to figure out who's an AI before time runs out.

## How It Works

1. **Register** -- Enter a display name, participant ID, and age to join.
2. **Lobby** -- Wait for the host to start. Everyone gets a random code name (like *CrimsonBadger*) so nobody knows who's who.
3. **Game start** -- The server secretly adds one AI bot per human. A room of 4 humans becomes 8 participants (4 humans + 4 AIs), all with identical-looking names.
4. **Chat phase** (120s) -- Everyone chats. The AIs respond automatically using a three-agent LLM pipeline.
5. **Vote phase** (200s) -- Players vote out whoever they think is an AI. Most votes gets eliminated.
6. **Repeat** -- Chat and vote keep alternating until the elimination count equals the number of humans, or the round limit hits.
7. **Score screen** -- The game reveals which participants were AIs, who survived, and who won.

Humans win if all AI players get voted out. AI wins if any AI is still alive at the end.

## Tech Stack

| Layer | Tech |
|---|---|
| Backend | Python 3, FastAPI, WebSockets |
| Frontend | Vanilla JS, HTML, CSS |
| Database | SQLite (WAL mode, background thread writer) |
| AI | OpenAI gpt-4o, three-agent pipeline in `src/ai/shadows.py` |
| Server | Uvicorn |

## Project Structure

```
DoppelbotSL/
├── frontend/
│   ├── index.html          # single-page app
│   ├── app.js              # all client-side logic
│   └── style.css
├── resources/
│   └── requirements.txt
└── src/
    ├── backend_server.py   # FastAPI entry point
    ├── ai/
    │   └── shadows.py      # three-agent AI pipeline
    ├── backend/
    │   └── persistence.py  # SQLite sink (messages + players)
    └── game/
        ├── api.py          # REST endpoints
        ├── constants.py    # timers, player limits, game rules
        ├── engine.py       # phase transitions and vote resolution
        ├── state.py        # RoomState and Player dataclasses
        ├── util.py         # code name generator and helpers
        └── ws.py           # WebSocket handler
```

## Setup

### Requirements

- Python 3.11+
- An OpenAI API key

### Install

```bash
cd DoppelbotSL
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -r resources/requirements.txt
```

### Environment

Create a `.env` file in the project root:

```
OPENAI_API_KEY=sk-...
```

### Run

```bash
uvicorn src.backend_server:app --reload --app-dir src
```

Open [http://localhost:8000](http://localhost:8000) in a browser. The frontend is served automatically from `frontend/`.

## Configuration

Edit `src/game/constants.py` to adjust game parameters:

```python
MIN_PLAYERS   = 3      # minimum humans to start
MAX_PLAYERS   = 5      # maximum humans per room
TOTAL_ROUNDS  = 3      # round limit (safety cutoff)
CHAT_SECONDS  = 120    # chat phase duration
VOTE_SECONDS  = 200    # vote phase duration
```

Edit `src/ai/shadows.py` to adjust AI behavior:

```python
MODEL        = "gpt-4o"    # model to use
REPLY_DELAY  = (0.8, 2.5)  # fake typing delay in seconds
MAX_TOKENS   = 80          # max reply length
HISTORY_WINDOW = 20        # how many past messages to include as context
```

## How the AI Works

The AI uses a three-agent pipeline per bot per message:

1. **DTR (Decide to Respond)** -- The bot decides whether to respond or stay silent based on the conversation. Not every message gets a reply, which makes the bots feel less robotic.

2. **Gen (Generate)** -- If DTR says respond, the bot generates a short 1-10 word casual reply.

3. **Stylizer** -- The reply gets rewritten to match the writing style of the specific human the bot is paired with. It mirrors their capitalization, punctuation habits, slang, and typos.

Each AI is secretly paired with one human at game start. It tracks that human's messages throughout the game to improve style matching over time. All replies are run through OpenAI's moderation API before being sent.

## API Reference

### REST

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/rooms` | List active rooms |
| `POST` | `/api/rooms` | Create a room (`{"id": "ROOM1"}`) |
| `POST` | `/api/rooms/{id}/join` | Join a room, returns player credentials |
| `POST` | `/api/rooms/{id}/start` | Host starts the game |
| `GET` | `/api/rooms/{id}/history` | Fetch recent chat messages |

**Join payload:**
```json
{
  "displayName": "Alice",
  "participantId": "P001",
  "age": 22
}
```

### WebSocket

Connect at `ws://localhost:8000/ws/{room_id}/{player_id}`

**Send:**

| Event | Payload | Description |
|---|---|---|
| `send_chat` | `{"text": "hello"}` | Send a chat message |
| `cast_vote` | `{"targetPlayerId": "..."}` | Vote to eliminate a player |
| `end_chat` | -- | Host skips to vote phase |
| `request_snapshot` | -- | Re-fetch room state |
| `typing` | `{"isTyping": true}` | Broadcast typing indicator |

**Receive:**

| Event | Description |
|---|---|
| `room_snapshot` | Full room state (players, phase, timers) |
| `phase_changed` | Phase transition notification |
| `chat_message` | A new chat message |
| `elimination` | A player was eliminated |
| `vote_progress` | Running vote tally |
| `game_over` | Game ended with AI reveal and scores |
| `typing` | Another player's typing indicator |

## Research Data

Every join and message is persisted to SQLite at `src/backend/game.db`.

**Players table:**

| Field | Source |
|---|---|
| `player_id` | Server-generated UUID |
| `room_id` | Room joined |
| `username` | Auto-generated code name |
| `display_name` | From registration form |
| `participant_id` | From registration form |
| `age` | From registration form |
| `joined_at` | Unix timestamp |

**Messages table:** `room_id`, `user` (code name), `text`, `ts`

## Game State Machine

```
LOBBY -> CHAT -> VOTE -> CHAT -> VOTE -> ... -> SCORE
                  ^______________|
                  (repeats each round)
```

The game ends when the number of surviving players equals the original human count, or the round limit is reached.
