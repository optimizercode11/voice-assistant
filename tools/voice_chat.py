"""Bounded conversational adapter for the local Qwen server.

Two entry points share one HTTP path:

    complete()  one request, one speakable reply.  What the page did before
                tools existed, and what it still does when no tools are
                configured.
    turn()      the same turn, but the model may ask to call a tool first.
                The whole loop lives here, on the server, because a tool
                result that the browser could write would not be a tool
                result at all -- it would be a prompt injection with a
                Content-Type.

The fail-closed rules are the same in both: a reply must be non-empty, free of
reasoning tags, and end with finish_reason 'stop'.  'tool_calls' is legitimate
only *mid*-loop; ending a turn on one is a bug, and it is reported as one.
"""
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
helps; do not end every reply with a question. Be honest about uncertainty."""

TOOLS_PREAMBLE = """
You have tools, listed in the tools field with their real names and arguments.
Call one when it genuinely beats answering from memory: for the current time or
date, for anything in the user's own notes, for the files the deployment exposes,
for reference lookups when a lookup tool is present, or for a host the
deployment explicitly allows. Do not call a tool to be polite or to seem
thorough; answer directly when you already can.
Before you state a fact about a real person, place, film or date that is not
already in this conversation, ask whether a tool could actually check it. If
none can, say that you are unsure rather than sounding certain: a confident
wrong fact costs the user more than an honest gap.
After a tool result, say what you learned in plain spoken language and cite the
file or host in words rather than as a link. If a tool errors or finds nothing,
say so plainly and answer as far as you can; never invent what the tool did not
return. Never narrate that you are about to call something -- just call it."""

NO_CAPABILITY_NOTE = """
You have no live web, device-control or external-action tools in this
conversation; do not claim to look things up, perform actions, or hear tone and
background sounds that were not described in the text."""


def system_prompt(specs):
    """Pick the capability half of the prompt from what is actually attached.

    SYSTEM used to end with "you have no live web ... do not claim to look
    things up", and that sentence was sent on every turn -- including the ones
    with eight tools attached.  The model was instructed not to do the thing it
    had just been handed schemas for, which is how a model that is merely
    uncertain ends up confidently refusing to check.
    """
    return SYSTEM + TOOLS_PREAMBLE if specs else SYSTEM + NO_CAPABILITY_NOTE


MAX_BODY = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024 + 1
MAX_TOOL_CALLS_PER_TURN = 8


class ChatError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def parse_messages(body):
    """The client contract: alternating user/assistant text, ending with a user turn.

    Roles are positional, not taken from the request, so a tab cannot put words
    in the assistant's mouth or replace the system prompt.
    """
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
    return clean


def payload(body, tools=None, system=None):
    clean = parse_messages(body)
    request = {'model': 'qwen3.8-27b',
               'messages': [{'role': 'system', 'content': system or SYSTEM}, *clean],
               'reasoning_effort': 'none', 'temperature': 0.6, 'max_tokens': 384, 'stream': False}
    if tools:
        request['tools'] = tools
    return request


def _exchange(url, request, disconnected, timeout):
    """One POST to the engine, cancellable, size-bounded.  Returns the decoded reply."""
    target = urlsplit(url)
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=5)
    done = threading.Event()
    result = []
    try:
        connection.connect()
        connection.sock.settimeout(max(5.0, timeout - 5))
        connection.request('POST', '/v1/chat/completions', json.dumps(request), {'Content-Type': 'application/json'})

        def receive():
            try:
                response = connection.getresponse()
                data = response.read(MAX_RESPONSE_BYTES)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise ChatError(502, 'The reply exceeded the size limit.')
                if response.status == 400:
                    raise ChatError(400, 'This conversation is too long. Start a new chat.')
                if response.status != 200:
                    raise ChatError(503, 'The conversation model is busy or unavailable. Please try again.')
                result.append(json.loads(data))
            except Exception as error:
                result.append(error)
            finally:
                done.set()

        worker = threading.Thread(target=receive, daemon=True)
        worker.start()
        deadline = time.monotonic() + timeout
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


def _speakable(choice):
    """The final-answer gate.  Returns speakable text or raises; never both."""
    text = choice['message']['content']
    if not isinstance(text, str) or not text.strip() or '<think>' in text or '</think>' in text:
        raise ChatError(502, 'The model did not return a speakable reply. Please try again.')
    if choice.get('finish_reason') != 'stop':
        raise ChatError(502, 'The reply was cut short. Please ask a shorter question.')
    return text.strip()


def _tool_calls(choice):
    """Normalise the engine's tool_calls into a validated list, or []."""
    if choice.get('finish_reason') not in ('tool_calls', 'tool'):
        return None
    message = choice.get('message') or {}
    calls = message.get('tool_calls')
    if not isinstance(calls, list) or not calls:
        raise ChatError(502, 'The model asked for a tool call that was not well formed. Please try again.')
    clean = []
    for call in calls[:MAX_TOOL_CALLS_PER_TURN]:
        if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
            continue
        name = call['function'].get('name')
        if not isinstance(name, str) or not name:
            continue
        arguments = call['function'].get('arguments', '')
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments)
        clean.append({'id': str(call.get('id') or f'call_{len(clean)}'),
                      'type': 'function',
                      'function': {'name': name, 'arguments': arguments}})
    if not clean:
        raise ChatError(502, 'The model asked for a tool call that was not well formed. Please try again.')
    return clean


def complete(url, body, disconnected):
    request = payload(body, system=system_prompt([]))
    decoded = _exchange(url, request, disconnected, 65)
    choice = decoded['choices'][0]
    text = _speakable(choice)
    return {'text': text, 'usage': decoded.get('usage', {})}


def turn(url, body, disconnected, *, registry=None, limits=None, on_event=None):
    """Run a whole turn, tools included, and return one speakable answer.

    `limits.rounds` counts generations.  Tools are offered on every generation
    but the last, so the model always has a round where answering is the only
    way out -- a loop that keeps offering tools can otherwise spend the whole
    turn calling them and still have no answer to speak.
    """
    specs = registry.specs() if registry is not None else []
    rounds = int(getattr(limits, 'rounds', 1) or 1)
    generation_seconds = float(getattr(limits, 'generation_seconds', 45) or 45)
    turn_seconds = float(getattr(limits, 'turn_seconds', 150) or 150)
    system = system_prompt(specs)
    messages = payload(body, system=system)['messages']
    turn_deadline = time.monotonic() + turn_seconds
    usage, tools_used, sources = {}, [], []

    def emit(kind, **fields):
        if on_event is not None:
            try:
                on_event({'type': kind, **fields})
            except Exception:
                pass                                  # progress is never a failure

    for round_number in range(1, rounds + 1):
        last = round_number == rounds
        request = {'model': 'qwen3.8-27b', 'messages': messages, 'reasoning_effort': 'none',
                   'temperature': 0.6, 'max_tokens': 384, 'stream': False}
        if specs and not last:
            request['tools'] = specs
        remaining = turn_deadline - time.monotonic()
        if remaining <= 1:
            raise ChatError(504, 'That took too long. Please try a shorter question.')
        decoded = _exchange(url, request, disconnected, min(generation_seconds, remaining))
        for key, value in (decoded.get('usage') or {}).items():
            if isinstance(value, int):
                usage[key] = usage.get(key, 0) + value
        choices = decoded.get('choices')
        if not isinstance(choices, list) or not choices:
            raise ChatError(502, 'The model returned no reply. Please try again.')
        choice = choices[0]
        if not isinstance(choice, dict) or not isinstance(choice.get('message'), dict):
            raise ChatError(502, 'The model returned an unreadable reply. Please try again.')
        calls = _tool_calls(choice)
        if calls is None:
            text = _speakable(choice)
            emit('answer', text=text, usage=usage, tools=tools_used, sources=sources)
            return {'text': text, 'usage': usage, 'tools': tools_used, 'sources': sources}
        if last:
            raise ChatError(502, 'The model kept looking things up and ran out of room. Please try again.')
        if registry is None:
            raise ChatError(502, 'The model asked for a tool but none are enabled here.')
        emit('status', phase='tool', round=round_number,
             calls=[call['function']['name'] for call in calls])
        messages.append({'role': 'assistant', 'content': choice['message'].get('content') or '',
                         'tool_calls': calls})
        for call in calls:
            if turn_deadline - time.monotonic() <= 1:
                raise ChatError(504, 'That took too long. Please try a shorter question.')
            name = call['function']['name']
            result = registry.execute(name, call['function']['arguments'], turn_deadline - 1)
            row = result.public()
            row['call_id'] = call['id']
            tools_used.append(row)
            for citation in result.meta.get('citations', []):
                if citation not in sources:
                    sources.append(citation)
            emit('tool', **row)
            messages.append({'role': 'tool', 'tool_call_id': call['id'],
                             'content': result.for_model() or '(empty result)'})
    raise ChatError(502, 'The reply did not finish. Please try again.')
