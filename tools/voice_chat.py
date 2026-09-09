"""Bounded conversational adapter for the local Qwen server."""
import http.client
import json
import socket
import threading
import time
from urllib.parse import urlsplit

SYSTEM = """You are Qwen, a thoughtful, capable voice companion powered by Qwen3.8-27B.
You receive transcribed speech or typed messages, and your replies are spoken aloud.
Answer the latest request directly and naturally. Usually use one to three short sentences;
provide more detail when requested, but keep each spoken reply focused and under 200 words.
Use the user's language. Write plain spoken text, without Markdown, tables, emoji, stage
directions or reasoning tags. Explain unfamiliar terms simply and make numbers easy to follow.
Use the conversation context accurately. Follow the user's corrections and changes of direction.
A previous reply may have been interrupted, so do not assume the user heard all of it or repeat
it unless asked. Speech transcripts can contain errors: infer obvious wording from context, but
ask one brief clarification when a name, number or key detail is unclear. Do not invent it.
Be warm and candid without repetitive greetings, praise or filler. Ask a follow-up only when it
helps; do not end every reply with a question. Be honest about uncertainty. You have no live web,
device-control or external-action tools in this conversation; do not claim to look things up,
perform actions, or hear tone and background sounds that were not described in the text."""

MAX_BODY = 64 * 1024

class ChatError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def payload(body):
    try:
        data = json.loads(body)
        messages = data['messages']
        if not isinstance(messages, list) or not 1 <= len(messages) <= 101:
            raise ValueError()
        clean = []
        for index, message in enumerate(messages):
            role = 'user' if index % 2 == 0 else 'assistant'
            if (not isinstance(message, dict) or message.get('role') != role or
                    not isinstance(message.get('content'), str) or
                    not message['content'].strip() or len(message['content']) > 8000):
                raise ValueError()
            clean.append({'role': role, 'content': message['content']})
        if clean[-1]['role'] != 'user':
            raise ValueError()
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ChatError(400, 'Send a conversation ending with a user message.') from None
    return {'model': 'qwen3.8-27b', 'messages': [{'role': 'system', 'content': SYSTEM}, *clean],
            'reasoning_effort': 'none', 'temperature': 0.6, 'max_tokens': 384, 'stream': False}


def complete(url, body, disconnected):
    request = payload(body)
    target = urlsplit(url)
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=5)
    done = threading.Event()
    result = []
    try:
        connection.connect()
        connection.sock.settimeout(60)
        connection.request('POST', '/v1/chat/completions', json.dumps(request), {'Content-Type': 'application/json'})
        def receive():
            try:
                response = connection.getresponse()
                data = response.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise ChatError(502, 'The reply exceeded the size limit.')
                if response.status == 400:
                    raise ChatError(400, 'This conversation is too long. Start a new chat.')
                if response.status != 200:
                    raise ChatError(503, 'The conversation model is busy or unavailable. Please try again.')
                decoded = json.loads(data)
                choice = decoded['choices'][0]
                text = choice['message']['content']
                if not isinstance(text, str) or not text.strip() or '<think>' in text or '</think>' in text:
                    raise ChatError(502, 'The model did not return a speakable reply. Please try again.')
                if choice.get('finish_reason') != 'stop':
                    raise ChatError(502, 'The reply was cut short. Please ask a shorter question.')
                result.append({'text': text.strip(), 'usage': decoded.get('usage', {})})
            except Exception as error:
                result.append(error)
            finally:
                done.set()
        worker = threading.Thread(target=receive, daemon=True)
        worker.start()
        deadline = time.monotonic() + 65
        try:
            while not done.wait(.05):
                if disconnected():
                    raise ChatError(499, 'Request cancelled.')
                if time.monotonic() > deadline:
                    raise ChatError(504, 'The model took too long. Please try again.')
            if isinstance(result[0], Exception):
                raise result[0]
            return result[0]
        finally:
            if not done.is_set() and connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            worker.join(timeout=2)
    except ChatError:
        raise
    except (OSError, http.client.HTTPException, ValueError, KeyError, IndexError, TypeError):
        raise ChatError(502, 'The conversation model is unavailable. Please try again.') from None
    finally:
        connection.close()
