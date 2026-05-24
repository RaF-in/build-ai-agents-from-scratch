from pydantic import BaseModel, Field, TypeAdapter
from typing import Any, Literal, Annotated
import json
from pathlib import Path
import aiosqlite
import asyncio
import time
import datetime
from litellm import acompletion

class FcMetaData(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
class FunctionCallData(BaseModel):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
class FunctionResponseData(BaseModel):
    name: str
    response: dict[str, Any] = Field(default_factory=dict)

class TextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str

class FunctionCallPart(BaseModel):
    kind: Literal["function_call"] = "function_call"
    data: FunctionCallData
    tool_call_id: str

class FunctionResponsePart(BaseModel):
    kind: Literal["function_response"] = "function_response"
    data: FunctionResponseData
    tool_call_id: str
    fc_metadata: FcMetaData | None = None

SessionPart = Annotated[TextPart | FunctionCallPart | FunctionResponsePart, Field(discriminator="kind")]

class SessionEvent(BaseModel):
    type: Literal["session_event"] = "session_event"
    parts: list[SessionPart] = Field(default_factory=list)
    role: str

    def to_litellm_message(self) -> list[dict[str, Any]]:
        if self.role == "assistant":
            text_parts = [p for p in self.parts if isinstance(p, TextPart)]
            call_parts = [p for p in self.parts if isinstance(p, FunctionCallPart)]
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": text_parts[0].text if text_parts else None,
            }
            if call_parts:
                msg["tool_calls"] = [
                    {
                        "id": p.tool_call_id,
                        "type": "function",
                        "function": {
                            "name": p.data.name,
                            "arguments": json.dumps(p.data.args),
                        },
                    }
                    for p in call_parts
                ]
            return [msg]

        messages = []
        for part in self.parts:
            if isinstance(part, TextPart):
                messages.append({"role": self.role, "content": part.text})
            elif isinstance(part, FunctionResponsePart):
                messages.append({
                    "role": "tool",
                    "name": part.data.name,
                    "content": json.dumps(part.data.response),
                    "tool_call_id": part.tool_call_id,
                })
        return messages

class CompactionEvent(BaseModel):
    type: Literal["compaction"] = "compaction"
    parts: list[SessionPart] = Field(default_factory=list)
    role: str = "system"
    def to_litellm_message(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": "Here are the compacted conversation memory\n" + " ".join([part.text for part in self.parts if isinstance(part, TextPart)])}]
    
StoredEvent = Annotated[SessionEvent | CompactionEvent, Field(discriminator="type")]

INIT_SQL = """
CREATE TABLE IF NOT EXISTS messages (

    id INTEGER PRIMARY KEY AUTOINCREMENT,

    kind TEXT NOT NULL DEFAULT 'session',

    event_json TEXT NOT NULL,

    created_at REAL NOT NULL
);
"""

StoredEventAdapter = TypeAdapter(
    StoredEvent
)

class SessionManager:
    def __init__(self, basedir: str | Path | None, model_name: str):
        self.model_name = model_name
        self.basedir = Path(basedir).resolve() if basedir is not None else Path(__file__).resolve().parent
        self.db_path = self.basedir / "agent.db"
        self.memory_dir = self.basedir / "memory"
        self._conn: aiosqlite.Connection | None = None
        self.lock = asyncio.Lock()
        self.is_initialized = False
    async def initialize(self):
        if self.is_initialized and self._conn is not None: 
            return
        async with self.lock:
            if self.is_initialized and self._conn is not None: 
                return
            self._conn = await aiosqlite.connect(self.db_path)
            await self._conn.executescript(INIT_SQL)
            await self._conn.commit()
            self.is_initialized = True

    async def close(self):
        if self._conn is None: 
            return
        await self._conn.close()
        self.is_initialized = False
        self._conn = None
    async def db(self):
        if self._conn is None:
            raise RuntimeError("Connection is not established")
        return self._conn
    async def delete(self):
        if self._conn is None:
            raise 
        await self._conn.execute("delete from messages")
        await self._conn.commit()

    async def add_message(
        self,
        event: StoredEvent,
        created_at: float | None = None,
    ):

        conn = await self.db()

        created_at = (
            created_at or time.time()
        )

        await conn.execute(
            """
            INSERT INTO messages (
                kind,
                event_json,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (
                event.type,

                event.model_dump_json(),

                created_at,
            ),
        )

        await conn.commit()

    async def _latest_compaction_id(
        self,
    ) -> int | None:

        conn = await self.db()

        cursor = await conn.execute(
            """
            SELECT id
            FROM messages
            WHERE kind = 'compaction'
            ORDER BY id DESC
            LIMIT 1
            """
        )

        row = await cursor.fetchone()

        await cursor.close()

        if row is None:
            return None

        return int(row[0])

    def should_summarize(
        self,
        events: list[StoredEvent],
    ) -> bool:

        max_events_before_summary = 40

        used_ratio = min(
            len(events)
            / max_events_before_summary,

            1.0,
        )

        remaining_pct = round(
            (1.0 - used_ratio) * 100,
            1,
        )

        print(
            f"[Memory]: "
            f"{remaining_pct}% "
            f"context remaining"
        )

        return (
            len(events)
            > max_events_before_summary
        )
    async def load_messages(self) -> list[StoredEvent]:
        conn = await self.db()
        latest_com_id = await self._latest_compaction_id()
        if latest_com_id is not None: 
            cursor = await conn.execute("""
                select kind, event_json 
                from messages 
                where id >= ?
                order by id asc 
        """, (latest_com_id,))
        else:
            cursor = await conn.execute("""
                select kind, event_json from messages order by id asc
                                        
        """)
        rows = await cursor.fetchall()
        await cursor.close()
        events = []
        for row in rows:
            kind, event_json = row
            event_obj = json.loads(event_json)
            if "type" not in event_obj: 
                event_obj["type"] = kind or "session"
            
            events.append(StoredEventAdapter
            .validate_python(event_obj))
        if self.should_summarize(events):
            events = await self.generate_summary(events)
        return events
    @staticmethod
    def _map_events_for_compaction(
        events: list[StoredEvent],
    ) -> list[dict[str, Any]]:

        mapped = []

        for event in events:

            if not isinstance(
                event,
                SessionEvent,
            ):
                continue

            for part in event.parts:

                # -----------------------------------------
                # TEXT
                # -----------------------------------------

                if isinstance(part, TextPart):

                    if (
                        event.role == "user"
                        and part.text.strip()
                    ):

                        mapped.append({
                            "type": "user_text",

                            "text": part.text,
                        })

                    continue

                # -----------------------------------------
                # TOOL CALL
                # -----------------------------------------

                if isinstance(
                    part,
                    FunctionCallPart,
                ):

                    mapped.append({
                        "type": "tool_call",

                        "name": (
                            part.data.name
                        ),

                        "args": (
                            part.data.args
                        ),
                    })

                    continue

                # -----------------------------------------
                # TOOL RESPONSE
                # -----------------------------------------

                if isinstance(
                    part,
                    FunctionResponsePart,
                ):

                    call_name = (
                        part.fc_metadata.name
                        if part.fc_metadata is not None
                        else part.data.name
                    )

                    call_args = (
                        part.fc_metadata.args
                        if part.fc_metadata is not None
                        else {}
                    )

                    mapped.append({
                        "type": "tool_response",

                        "name": call_name,

                        "args": call_args,

                        "response": (
                            part.data
                            .response
                        ),
                    })

        return mapped

    # =====================================================
    # MAPPED EVENTS -> TEXT
    # =====================================================

    @staticmethod
    def _mapped_events_to_text(
        mapped_events: (
            list[dict[str, Any]]
        ),
    ) -> str:

        lines = ["<conversation>"]

        for entry in mapped_events:

            # ---------------------------------------------
            # USER TEXT
            # ---------------------------------------------

            if entry["type"] == "user_text":

                lines.append(
                    f"[user]: "
                    f"{entry['text']}"
                )

                continue

            # ---------------------------------------------
            # TOOL CALL
            # ---------------------------------------------

            if entry["type"] == "tool_call":

                lines.append(
                    f"[model]: "
                    f"Tool call "
                    f"`{entry['name']}` "
                    f"with args "
                    f"{entry['args']}"
                )

                continue

            # ---------------------------------------------
            # TOOL RESPONSE
            # ---------------------------------------------

            if entry["type"] == "tool_response":

                lines.append(
                    f"[model]: "
                    f"Tool response "
                    f"`{entry['name']}` "
                    f"with args "
                    f"{entry['args']} "
                    f"returned "
                    f"{entry['response']}"
                )

        lines.append("</conversation>")

        return "\n".join(lines)

    # =====================================================
    # SAVE MEMORY FILE
    # =====================================================

    def _append_summary_to_memory(
        self,
        summary_text: str,
    ):

        now = datetime.datetime.now()

        self.memory_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        file_name = (
            self.memory_dir
            / f"{now.strftime('%d-%m-%Y')}.md"
        )

        line = (
            f"[{now.strftime('%H:%M')}]: "
            f"{summary_text}\n"
        )

        with open(
            file_name,
            "a",
            encoding="utf-8",
        ) as f:

            f.write(line)

    # =====================================================
    # GENERATE SUMMARY
    # =====================================================

    async def generate_summary(
        self,
        events: list[StoredEvent],
    ) -> list[StoredEvent]:

        if not events:
            return events

        # -------------------------------------------------
        # MAP EVENTS
        # -------------------------------------------------

        mapped_events = (
            self._map_events_for_compaction(
                events
            )
        )

        if not mapped_events:
            return events

        # -------------------------------------------------
        # CONVERT TO TEXT
        # -------------------------------------------------

        mapped_conversation = (
            self._mapped_events_to_text(
                mapped_events
            )
        )

        print(
            "[Memory] "
            "Compaction Started"
        )

        # -------------------------------------------------
        # LITELLM SUMMARIZATION CALL
        # -------------------------------------------------

        response = await acompletion(
            model=self.model_name,

            messages=[
                {
                    "role": "system",

                    "content": """
You're about to be given mapped interactions from a conversation.

Your job is to generate a summary of the conversation in at most 4 paragraphs.

Required structure:

1. Start with the key user objective first.

2. Then describe what was accomplished.

3. Then briefly cover blockers and what was learned.

Rules:
- Return natural language only
- No markdown
- No JSON
- No hallucinations
- Be concise
"""
                },

                {
                    "role": "user",

                    "content": (
                        "Summarize this conversation:\n\n"
                        + mapped_conversation
                    ),
                },
            ],

            temperature=0.2,
        )

        summary = (
            response
            .choices[0]
            .message
            .content
            .strip()
        )

        print(
            "[Memory] "
            "Compaction Finished"
        )

        print(summary)

        # -------------------------------------------------
        # SAVE MEMORY FILE
        # -------------------------------------------------

        self._append_summary_to_memory(
            summary
        )

        # -------------------------------------------------
        # CREATE SUMMARY EVENT
        # -------------------------------------------------

        summary_event = CompactionEvent(
            parts=[TextPart(text=summary)]
        )

        # -------------------------------------------------
        # SAVE TO DATABASE
        # -------------------------------------------------

        await self.add_message(
            summary_event
        )

        # -------------------------------------------------
        # RETURN COMPACTED CONTEXT
        # -------------------------------------------------

        return [summary_event]

    # =====================================================
    # CONVERT STORED EVENTS -> LITELLM
    # =====================================================

    @staticmethod
    def events_to_litellm_messages(
        events: list[StoredEvent],
    ) -> list[dict[str, Any]]:

        messages = []

        for event in events:

            messages.extend(
                event.to_litellm_message()
            )

        return messages
