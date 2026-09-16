"""
server.py
FastAPI backend — exposes the agent over HTTP with Server-Sent Events (SSE)
so the web UI can show live tool-call progress, not just the final answer.
"""

import json
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from bedrock_agent import ask_agent_stream

app = FastAPI(title="Chat with Your Cluster")


class ChatRequest(BaseModel):
    question: str


@app.post("/chat")
async def chat(request: ChatRequest):
    """
    Streams agent events (tool_call, tool_result, final) as Server-Sent Events.
    Frontend listens to this stream and renders each step live.
    """
    def event_generator():
        for event in ask_agent_stream(request.question):
            # SSE format: each message prefixed with "data: " and ending in double newline
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# Serve the frontend (index.html + any static assets) from ./static
app.mount("/", StaticFiles(directory="static", html=True), name="static")