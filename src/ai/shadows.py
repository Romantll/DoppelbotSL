import asyncio
import os
import random
from dataclasses import dataclass
from typing import Dict, Optional, Callable, Set

from dotenv import load_dotenv
from openai import AsyncOpenAI

from game.constants import PHASE_CHAT
from game.util import generate_username

load_dotenv()

# ---- Tuning knobs ----
MODEL           = "gpt-4o"        # swap to "gpt-4o-mini" for a cheaper/faster bot
RESPONSE_CHANCE = 0.65            # probability any given human message gets a reply
REPLY_DELAY     = (0.8, 2.5)      # seconds of fake "typing" delay (min, max)
MAX_TOKENS      = 80              # keep replies short and chat-like
HISTORY_WINDOW  = 20              # how many recent messages to include as context
# ----------------------


@dataclass
class ShadowAI:
    owner_player_id: str
    username: str  # unique code name shown in chat


class ShadowAIManager:
    """
    Manages AI bot responses for the game room.
    Wire up your LLM here — the rest of the server does not need to change.
    """

    def __init__(self, send_chat: Callable[[str, str, str], "asyncio.Future"]):
        self._send_chat = send_chat
        self._agents_by_owner: Dict[str, ShadowAI] = {}
        self._shadow_names: Set[str] = set()
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    def reset_for_room(self):
        self._agents_by_owner.clear()
        self._shadow_names.clear()

    def ensure_shadow(self, owner_player_id: str, owner_username: str,
                      human_taken: Set[str] = None) -> ShadowAI:
        if owner_player_id in self._agents_by_owner:
            return self._agents_by_owner[owner_player_id]

        taken = self._shadow_names | (human_taken or set())
        shadow_username = generate_username(taken)
        self._shadow_names.add(shadow_username)

        agent = ShadowAI(owner_player_id=owner_player_id, username=shadow_username)
        self._agents_by_owner[owner_player_id] = agent
        return agent

    def get_shadow(self, owner_player_id: str) -> Optional[ShadowAI]:
        return self._agents_by_owner.get(owner_player_id)

    async def on_room_message(
        self,
        room_id: str,
        human_sender_player_id: str,
        human_sender_username: str,
        human_text: str,
        room,
        conversation_history: list,   # [{"user": str, "text": str, "ts": int}, ...]
        game_rules: str,
    ):
        # Skip responding sometimes to avoid the bot feeling too reactive
        if random.random() > RESPONSE_CHANCE:
            return

        alive_ais = [p for p in room.players.values() if p.is_ai and not p.eliminated]
        if not alive_ais:
            return

        ai_player = random.choice(alive_ais)

        # Format the recent chat history as a plain transcript
        recent = conversation_history[-HISTORY_WINDOW:]
        transcript = "\n".join(f"{m['user']}: {m['text']}" for m in recent)

        messages = [
            {
                "role": "system",
                "content": (
                    f"You are secretly an AI playing a social deduction chat game called DoppelbotSL. "
                    f"Your username in this chat is '{ai_player.username}'. "
                    f"{game_rules}\n\n"
                    "Your goal is to blend in with the human players so they cannot vote you out. "
                    "Write exactly like a real person texting in a casual group chat. "
                    "Rules for style: use lowercase most of the time, skip unnecessary punctuation "
                    "(no period at the end of a sentence unless needed for clarity), "
                    "use contractions, drop filler words, keep it to one short sentence or a fragment. "
                    "Occasionally make a small typo or use 'lol', 'idk', 'tbh', 'ngl', 'lmao' naturally — "
                    "but do not overdo it. Never use formal punctuation like semicolons or em-dashes. "
                    "Ask short questions sometimes. React to what was just said. "
                    "Never admit you are an AI. Never break character.\n\n"
                    "You must follow these rules at all times: do not use profanity, slurs, or offensive "
                    "language of any kind. Do not discuss violence, sexual content, drugs, self-harm, "
                    "or any topic that would be inappropriate in a research setting. Keep all responses "
                    "clean, neutral, and suitable for all audiences. If the conversation steers toward "
                    "any of those topics, redirect with a neutral comment about the game."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Chat so far:\n{transcript}\n\n"
                    f"Reply as {ai_player.username}. One short message only."
                ),
            },
        ]

        # Simulate a human typing delay before sending
        await asyncio.sleep(random.uniform(*REPLY_DELAY))

        # Phase may have changed while we were sleeping (e.g. chat timer expired)
        if room.phase != PHASE_CHAT:
            return

        try:
            response = await self._client.chat.completions.create(
                model=MODEL,
                messages=messages,
                max_tokens=MAX_TOKENS,
                temperature=1.1,
            )
            reply = response.choices[0].message.content.strip()
            if not reply:
                return

            # Run the reply through OpenAI's moderation endpoint before sending.
            # This catches anything that slipped through the system prompt.
            mod = await self._client.moderations.create(input=reply)
            if mod.results[0].flagged:
                print(f"[ShadowAI] Moderation blocked reply from {ai_player.username}: {reply!r}")
                return

            await self._send_chat(room_id, ai_player.username, reply)
        except Exception as e:
            print(f"[ShadowAI] OpenAI call failed: {e}")
