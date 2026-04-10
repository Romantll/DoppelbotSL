import asyncio
import os
import re
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Callable

from dotenv import load_dotenv
from openai import AsyncOpenAI

from game.constants import PHASE_CHAT

load_dotenv()

# ---- Tuning knobs ----
MODEL        = "gpt-4o"      # swap to "gpt-4o-mini" for cheaper/faster
REPLY_DELAY  = (0.8, 2.5)   # seconds of fake "typing" delay (min, max)
MAX_TOKENS   = 80            # keep replies short and chat-like
HISTORY_WINDOW = 20          # how many recent messages to include as context
# ----------------------


@dataclass
class ShadowAI:
    owner_player_id: str
    username: str


class ShadowAIManager:
    """
    Manages AI bot responses using a three-agent pipeline:
      1. DTR  — Decide-to-Respond
      2. Gen  — Generate a raw reply
      3. Stylize — Rewrite to match the paired human's writing style
    """

    def __init__(self, send_chat: Callable[[str, str, str], "asyncio.Future"]):
        self._send_chat = send_chat
        self._client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        # ai_pid -> human_pid (set at game start)
        self._pairings: Dict[str, str] = {}
        # human_pid -> list of that human's message texts
        self._human_messages: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset_for_room(self, pairings: Dict[str, str]):
        """
        Call at the start of each game.
        pairings: {ai_player_id: human_player_id}
        """
        self._pairings = dict(pairings)
        self._human_messages = {human_pid: [] for human_pid in pairings.values()}

    # ------------------------------------------------------------------
    # Public hook — called after every human chat message
    # ------------------------------------------------------------------

    async def on_room_message(
        self,
        room_id: str,
        human_sender_player_id: str,
        human_sender_username: str,
        human_text: str,
        room,
        conversation_history: list,  # [{"user": str, "text": str, "ts": int}, ...]
        game_rules: str,
    ):
        # Track this human's messages for style matching
        if human_sender_player_id in self._human_messages:
            self._human_messages[human_sender_player_id].append(human_text)

        # Run each AI's pipeline independently (non-blocking)
        alive_ais = [
            (ai_pid, p)
            for ai_pid, p in room.players.items()
            if p.is_ai and not p.eliminated
        ]
        for ai_pid, ai_player in alive_ais:
            asyncio.create_task(
                self._pipeline(
                    room_id=room_id,
                    ai_pid=ai_pid,
                    ai_username=ai_player.username,
                    room=room,
                    conversation_history=conversation_history,
                    game_rules=game_rules,
                )
            )

    # ------------------------------------------------------------------
    # Three-agent pipeline
    # ------------------------------------------------------------------

    async def _pipeline(
        self,
        room_id: str,
        ai_pid: str,
        ai_username: str,
        room,
        conversation_history: list,
        game_rules: str,
    ):
        recent = conversation_history[-HISTORY_WINDOW:]
        transcript = "\n".join(f"{m['user']}: {m['text']}" for m in recent)

        # Stage 1: DTR
        should_respond = await self._dtr(
            ai_username=ai_username,
            transcript=transcript,
            game_rules=game_rules,
        )
        if not should_respond:
            return

        # Simulate typing delay
        await asyncio.sleep(random.uniform(*REPLY_DELAY))

        # Phase may have changed while sleeping
        if room.phase != PHASE_CHAT:
            return

        # Stage 2: Generate raw reply
        raw_reply = await self._gen(
            ai_username=ai_username,
            transcript=transcript,
            game_rules=game_rules,
        )
        if not raw_reply:
            return

        # Stage 3: Stylize to match paired human
        human_pid = self._pairings.get(ai_pid)
        human_msgs = self._human_messages.get(human_pid, []) if human_pid else []
        final_reply = await self._stylize(raw_reply, human_msgs)
        if not final_reply:
            return

        # Moderation check before sending
        try:
            mod = await self._client.moderations.create(input=final_reply)
            if mod.results[0].flagged:
                print(f"[ShadowAI] Moderation blocked reply from {ai_username}: {final_reply!r}")
                return
        except Exception as e:
            print(f"[ShadowAI] Moderation check failed: {e}")
            return

        await self._send_chat(room_id, ai_username, final_reply)

    # ------------------------------------------------------------------
    # Agent 1: Decide-to-Respond (DTR)
    # ------------------------------------------------------------------

    async def _dtr(self, ai_username: str, transcript: str, game_rules: str) -> bool:
        system_prompt = (
            f"You are secretly an AI playing a social deduction chat game called DoppelbotSL. "
            f"Your username in this chat is '{ai_username}'. "
            f"{game_rules}\n\n"
            "Your goal is to blend in with the human players so they cannot vote you out. "
            "Decide whether you should respond to the current conversation or stay silent.\n\n"
            "Rules for this decision:\n"
            "- Do NOT respond to every message. Real people stay quiet sometimes.\n"
            "- If the message is not addressed to you and you have nothing natural to add, stay silent.\n"
            "- If you just responded recently, lean toward staying silent.\n"
            "- If the conversation is interesting and you have something short to add, respond.\n\n"
            "You must follow these content rules at all times: do not use profanity, slurs, or offensive "
            "language. Do not discuss violence, sexual content, drugs, self-harm, or any topic "
            "inappropriate in a research setting. Keep all responses clean and suitable for all audiences.\n\n"
            'Reply with exactly this format: I will "RESPOND" because I think that ***<reason>*** '
            'or I will "STAY SILENT" because I think that ***<reason>***'
        )
        user_prompt = f"Chat so far:\n{transcript}\n\nDecide whether to respond as {ai_username}."

        try:
            response = await self._client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=60,
                temperature=0.7,
            )
            text = response.choices[0].message.content or ""
            # Look for RESPOND vs STAY SILENT in the output
            match = re.search(r'"(RESPOND|STAY SILENT)"', text, re.IGNORECASE)
            if match:
                return match.group(1).upper() == "RESPOND"
            # Fallback: if we can't parse it, default to staying silent
            return False
        except Exception as e:
            print(f"[ShadowAI] DTR call failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Agent 2: Generate raw reply
    # ------------------------------------------------------------------

    async def _gen(self, ai_username: str, transcript: str, game_rules: str) -> Optional[str]:
        system_prompt = (
            f"You are secretly an AI playing a social deduction chat game called DoppelbotSL. "
            f"Your username in this chat is '{ai_username}'. "
            f"{game_rules}\n\n"
            "Your goal is to blend in with the human players so they cannot vote you out. "
            "Write a short, casual message like a real person texting in a group chat. "
            "Keep it to 1-10 words. Lowercase, no unnecessary punctuation, natural and human-sounding. "
            "Never admit you are an AI. Never break character.\n\n"
            "You must follow these content rules at all times: do not use profanity, slurs, or offensive "
            "language. Do not discuss violence, sexual content, drugs, self-harm, or any topic "
            "inappropriate in a research setting. Keep all responses clean and suitable for all audiences. "
            "If the conversation steers toward those topics, redirect with a neutral comment about the game.\n\n"
            'Reply with exactly this format: My response is as follows "{RESPONSE}"'
        )
        user_prompt = (
            f"Chat so far:\n{transcript}\n\n"
            f"Reply as {ai_username}. One short message only."
        )

        try:
            response = await self._client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=MAX_TOKENS,
                temperature=1.1,
            )
            text = response.choices[0].message.content or ""
            # Extract the response from the format
            match = re.search(r'My response is as follows ["\u201c](.+?)["\u201d]', text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
            # Fallback: use the raw text if format not followed
            return text.strip() or None
        except Exception as e:
            print(f"[ShadowAI] Gen call failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Agent 3: Stylize to match paired human's writing style
    # ------------------------------------------------------------------

    async def _stylize(self, raw_reply: str, human_messages: List[str]) -> Optional[str]:
        if human_messages:
            example_block = "\n".join(f"- {m}" for m in human_messages[-10:])
            style_context = (
                f"Here are recent messages from the person whose style you should match:\n"
                f"{example_block}\n\n"
                "Match their capitalization, spelling patterns, punctuation habits, and phrasing exactly. "
                "If they use typos, use similar ones. If they never capitalize, don't capitalize. "
                "If they use slang, mirror it naturally."
            )
        else:
            style_context = (
                "Default style: all lowercase, no punctuation at the end of sentences, "
                "occasional natural typos, casual and brief."
            )

        system_prompt = (
            "You are a text style rewriter. Rewrite the given message to match a specific person's "
            "writing style without changing the meaning. Output only the rewritten message — "
            "no explanation, no quotes, nothing else.\n\n"
            f"{style_context}"
        )
        user_prompt = f"Rewrite this message in that style: {raw_reply}"

        try:
            response = await self._client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=MAX_TOKENS,
                temperature=0.8,
            )
            result = (response.choices[0].message.content or "").strip()
            # Strip surrounding quotes if the model added them
            result = result.strip('"').strip("'").strip("\u201c\u201d")
            return result or None
        except Exception as e:
            print(f"[ShadowAI] Stylize call failed: {e}")
            return raw_reply  # fall back to raw reply if stylizer fails
