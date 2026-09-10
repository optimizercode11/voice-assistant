#!/usr/bin/env python3
"""Same-origin speech UI, streaming Kokoro proxy, and bounded native ASR bridge.

Run on the GPU host through guarded-hostrun. The ASR process inherits the
guard's device, affinity, priority and process group. No model imports here.
"""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import voice_chat
import agent_config
import agent_tools
import approvals
import turn_control
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import wave

MAX_BYTES = 20 * 1024 * 1024
MAX_SECONDS = 120
TIMEOUT = 270


class RequestError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


# The qasr engine sometimes keeps decoding after the utterance has ended and
# emits a run of one token: "tomorrow..............." or "museum...ory.ory.ory".
# It is not a transcription of anything -- the words before it are correct and
# the run is the same token repeated.  See PROVENANCE.md for the reproduction
# against known ground truth.  The engine fix belongs in qasr; this is the
# guard that stops it reaching a voice.
PUNCT_RUN = re.compile(r'([.,:;!?-])\1{2,}')
TAIL_PUNCT_RUN = re.compile(r'[.,:;!?-]{2,}\s*$')
TAIL_UNIT_MAX = 12
TAIL_MIN_REPEATS = 3
TAIL_MIN_CHARS = 6            # "haha" and "very very" are not this bug
PUNCT_CHARS = ".,:;!?-\u2013\u2014 "


def collapse_repeated_tail(text):
    """Fold a degenerate repeated tail back down.

    Two shapes, because the engine produces both: a run of one punctuation
    character ("tomorrow...............") and a short unit repeated at the very
    end ("museums...ory.ory.ory.ory").  The second is the interesting one --
    the unit is not a single character, so a naive dedupe misses it entirely.

    A repeated unit that contains letters is a hallucinated word fragment, not
    content, so the whole run goes.  A repeated unit that is pure punctuation
    keeps one copy: it is harmless and it preserves sentence-final prosody.

    Deliberately conservative.  It only ever shortens a *tail*, needs three
    repeats and six characters, and never touches repetition earlier in the
    sentence, where "no no no" is something a person actually said.
    """
    if not text:
        return text
    body = text.rstrip()
    trailing = text[len(body):]
    worst = None
    for length in range(1, TAIL_UNIT_MAX + 1):
        if len(body) < length * TAIL_MIN_REPEATS:
            break
        unit = body[-length:]
        repeats, position = 0, len(body)
        while position >= length and body[position - length:position] == unit:
            position -= length
            repeats += 1
        if repeats >= TAIL_MIN_REPEATS and length * repeats >= TAIL_MIN_CHARS:
            if worst is None or length * repeats > worst[0]:
                worst = (length * repeats, unit, position)
    if worst is not None:
        _, unit, position = worst
        keep = unit if all(character in PUNCT_CHARS for character in unit) else ""
        body = body[:position] + keep
    body = PUNCT_RUN.sub(r"\1", body)
    # A tail of mixed punctuation ("month,,,,,,.,,,") is the same defect with
    # the tokens interleaved, so fold the whole trailing run to one character.
    body = TAIL_PUNCT_RUN.sub(lambda match: match.group(0)[0], body)
    return body + trailing


def transcript_text(raw):
    """Remove native speaker headers, silence markers and degenerate ASR tails."""
    text = raw.replace('[Silence]', '')
    text = re.sub(r'(^|\n)[ \t]*Speaker[ \t]+\d+[ \t]*:[ \t]*', r'\1', text)
    return collapse_repeated_tail(text).strip()


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Bounded escalation applies only to this exact, owned child.
            process.kill()
            process.wait()


def execute(command, directory, timeout, disconnected):
    """File-backed output avoids pipe deadlocks; preserve the guard's PGID."""
    with tempfile.TemporaryFile(dir=directory) as stdout, tempfile.TemporaryFile(dir=directory) as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if disconnected():
                    raise RequestError(499, "Request cancelled.")
                if time.monotonic() > deadline:
                    raise RequestError(504, "Transcription timed out. Try a shorter recording.")
                if stdout.tell() > 8 * 1024 * 1024 or stderr.tell() > 8 * 1024 * 1024:
                    raise RequestError(502, "Speech process exceeded its output limit.")
                time.sleep(.05)
            stdout.seek(0)
            output = stdout.read(8 * 1024 * 1024)
            if process.returncode:
                # Keep native paths/logs out of the browser response.
                stderr.seek(0)
                print(stderr.read(4096).decode("utf-8", "replace"), flush=True)
                raise RequestError(502, "Speech processing failed. Check the server log.")
            return output
        finally:
            stop_process(process)


def speech_ms(headers):
    """How long the browser actually heard a voice, in milliseconds, or None.

    The browser owns the microphone and knows this exactly; the bridge cannot
    recover it from the clip, because the clip also contains the silence the
    endpointer insists on before it stops.  It is a hint and not a claim: it
    only ever makes the assistant wait longer, never cut a turn short, and a
    missing or absurd value falls back to judging the words alone.

    A header rather than a query parameter, so /stt stays one URL.  The browser
    suites route on that URL, and a glob like `**/stt` does not match a URL that
    grew a query string -- a diagnostic that quietly breaks the tests is a
    diagnostic that gets deleted later.
    """
    try:
        value = int(str(headers.get("X-Voiced-Ms", "")).strip())
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= MAX_SECONDS * 1000 else None


def transcribe_remote(config, wav_bytes, frames, disconnected, speech_ms_value=None):
    """Hand the decoded WAV to the resident qasr server; keep the local contract.

    WHY THE BRIDGE STILL OWNS FFMPEG AND THE CAPS
        The browser-facing contract (text, raw_text, audio_seconds, chunks) and
        the byte/second ceilings do not belong to a model, so they stay here and
        vvasr keeps working unchanged.  What the swap removes is the per-request
        model load: qasr_serve holds the checkpoint resident, so a request costs
        the engine's own ~40-100 ms instead of a process spawn plus a load.

    `wav_bytes` is ffmpeg's output (24 kHz mono s16), not the browser's upload:
    the pinned librosa resampler is what the oracle used, so the certified path
    is decode-to-24 kHz then resample, and the bridge keeps owning that.

    The engine has one arena and this handler already holds asr_lock, so exactly
    one request is in flight either way.  A dead or wedged server is a 503/502
    with a name, never a silent empty transcript.
    """
    target = urlsplit(config.asr_url)
    if disconnected():
        raise RequestError(499, "Request cancelled.")
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=TIMEOUT)
    try:
        connection.request("POST", "/stt", wav_bytes, {"Content-Type": "audio/wav"})
        response = connection.getresponse()
        payload = response.read(4 * 1024 * 1024)
        status = response.status
    except (OSError, http.client.HTTPException) as error:
        raise RequestError(503, "The speech-to-text engine is unavailable.") from error
    finally:
        connection.close()
    if disconnected():
        raise RequestError(499, "Request cancelled.")
    # Status first, body second.  Reading the body before the status turned a
    # dead engine (503, empty body) into "The ASR engine returned unreadable
    # output" -- still a 5xx, but it blames the engine's grammar for an outage,
    # which is the wrong thing to put in front of an operator at 3am.
    detail = ""
    if payload:
        try:
            parsed = json.loads(payload.decode("utf-8"))
            if isinstance(parsed, dict):
                detail = str(parsed.get("error") or "")
        except (UnicodeDecodeError, ValueError):
            parsed = None
    else:
        parsed = None
    if status != 200:
        if status >= 500:
            raise RequestError(503, detail or "The speech-to-text engine is unavailable.")
        raise RequestError(status, detail or "The ASR engine refused this audio.")
    result = parsed
    if not isinstance(result, dict):
        raise RequestError(502, "The ASR engine returned unreadable output.")
    text, raw = result.get("text"), result.get("raw_text")
    if not isinstance(text, str) or not isinstance(raw, str):
        raise RequestError(502, "The ASR engine returned incomplete output.")
    cleaned = transcript_text(text)
    reply = {"text": cleaned, "raw_text": raw,
             "audio_seconds": frames / 24000, "chunks": int(result.get("chunks", 1)),
             # Is this actually a finished turn?  The browser owns the microphone
             # and cannot tell "I" from "I want...", so the transcript plus the
             # voiced duration the browser measured say so.
             "turn": turn_control.completeness(cleaned, speech_ms_value)}
    for key in ("engine", "frontend_ms", "frames"):     # diagnostics, not contract
        if key in result:
            reply[key] = result[key]
    return reply


def transcribe(config, audio, directory, disconnected, speech_ms_value=None):
    source, wav = Path(directory) / "upload", Path(directory) / "audio.wav"
    source.write_bytes(audio)
    # Whitelist media demuxers; uploaded playlists must not read local files or
    # remote URLs. Decode a bounded prefix plus one second to reject long audio.
    command = [config.ffmpeg, "-nostdin", "-v", "error", "-threads", "1",
               "-protocol_whitelist", "file,pipe", "-format_whitelist",
               "wav,mp3,flac,ogg,mov,matroska,webm,aac", "-i", str(source),
               "-map", "0:a:0", "-vn", "-t", str(MAX_SECONDS + 1),
               "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
               "-threads", "1", "-y", str(wav)]
    try:
        execute(command, directory, 30, disconnected)
    except RequestError as error:
        if error.status == 502:
            raise RequestError(400, "Cannot decode this audio. Upload WAV, MP3, M4A, Ogg, WebM or FLAC.") from error
        raise
    with wave.open(str(wav), "rb") as reader:
        frames = reader.getnframes()
        if not frames or frames > MAX_SECONDS * 24000:
            raise RequestError(400, "Audio must contain between 0 and 120 seconds of samples.")
    if getattr(config, "asr_url", None):
        # Decode first, then hand the server the same 24 kHz mono s16 WAV the
        # native path was given: the browser uploads webm/opus, which no ASR
        # frontend reads directly, and the certified path is ffmpeg-to-24 kHz
        # followed by the pinned librosa resample.
        return transcribe_remote(config, wav.read_bytes(), frames, disconnected, speech_ms_value)
    output = execute([str(config.asr_bin), "--device", "cuda", "--model", str(config.model),
                      "--tokenizer", str(config.tokenizer), "--wav", str(wav),
                      "--seed", "1729", "--max-tokens", "256"], directory, TIMEOUT, disconnected)
    try:
        chunks = [json.loads(line) for line in output.decode("utf-8").splitlines() if line.strip()]
        expected = (frames + 70400 - 1) // 70400
        if len(chunks) != expected:
            raise ValueError("missing or extra chunks")
        for index, chunk in enumerate(chunks):
            if (chunk.get("chunk") != index or chunk.get("name") != str(wav)
                    or not isinstance(chunk.get("text"), str)
                    or len(chunk.get("tokens", [])) >= 256):
                raise ValueError("invalid chunk or truncated output")
        raw = "".join(chunk["text"] for chunk in chunks)
        cleaned = transcript_text(raw)
        return {"text": cleaned, "raw_text": raw,
                "audio_seconds": frames / 24000, "chunks": len(chunks),
                "turn": turn_control.completeness(cleaned, speech_ms_value)}
    except (ValueError, TypeError, AttributeError) as error:
        raise RequestError(502, "The ASR engine returned incomplete or invalid output.") from error


class Progress:
    """Chunked NDJSON: the page learns a tool is running instead of staring at a spinner.

    Only phase names, timings and citations travel here -- never the tool's
    raw output, which the model has already been given and the browser has no
    business re-deciding.  The stream is opt-in per request, so every client
    that expects one JSON body keeps getting exactly that.
    """

    def __init__(self, handler):
        self.handler = handler
        handler.send_response(200)
        handler.send_header("Content-Type", "application/x-ndjson")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Accel-Buffering", "no")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        self.open = True

    def _write(self, data: bytes) -> None:
        # The socket is nonblocking while we poll for a dead tab, so each write
        # borrows a bounded blocking window: a peer that stopped reading costs
        # five seconds, not a wedged handler thread.
        connection = self.handler.connection
        try:
            connection.settimeout(5)
            self.handler.wfile.write(data)
            self.handler.wfile.flush()
        finally:
            connection.settimeout(0)

    def send(self, event: dict) -> None:
        if not self.open:
            return
        payload = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self._write(f"{len(payload):x}\r\n".encode() + payload + b"\r\n")
        except (OSError, ValueError):
            self.open = False                        # the tab went away; the turn still finishes

    def close(self) -> None:
        if not self.open:
            return
        self.open = False
        try:
            self._write(b"0\r\n\r\n")
        except (OSError, ValueError):
            pass


def approvals_store(registry):
    """The directory-grant queue for this bridge, or None when it is switched off.

    Derived from the registry rather than passed around, because there are two
    listeners (plain and TLS) and a store per listener would mean a grant made
    on one is invisible on the other -- and the microphone only ever uses the
    TLS one.
    """
    if registry is None:
        return None
    return agent_tools.approvals_store(registry.config, registry.context.root)


class SpeechServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, config, *, tls=None, asr_lock=None, chat_lock=None, registry=None):
        self.config = config
        self.registry = registry
        self.tls = tls
        self.chat_lock = chat_lock if chat_lock is not None else threading.Lock()
        self.asr_lock = asr_lock if asr_lock is not None else threading.Lock()
        super().__init__(address, Handler)

    def finish_request(self, request, client_address):
        if self.tls is None:
            return super().finish_request(request, client_address)
        # Handshake in the request thread, so a stalled TLS client cannot block
        # other clients from connecting to the listener.
        request.settimeout(10)
        try:
            with self.tls.wrap_socket(request, server_side=True) as secured:
                super().finish_request(secured, client_address)
        except (ssl.SSLError, OSError):
            request.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        self.response_started = False
        self.body_consumed = False

    def send_response(self, code, message=None):
        self.response_started = True
        super().send_response(code, message)

    def reply(self, status, body, content_type="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        return not origin or urlsplit(origin).netloc == self.headers.get("Host")

    def body(self, limit):
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "Chunked uploads are not supported.")
        value = self.headers.get("Content-Length", "")
        if not value.isascii() or not value.isdigit():
            raise RequestError(411, "A Content-Length is required.")
        count = int(value)
        if not count or count > limit:
            raise RequestError(413, "Upload is empty or exceeds the size limit.")
        result = self.rfile.read(count)
        if len(result) != count:
            raise RequestError(400, "Incomplete upload.")
        self.body_consumed = True
        return result

    def disconnected(self):
        try:
            # The entire request body has been consumed and responses close the
            # connection. SSL sockets reject MSG_PEEK/MSG_DONTWAIT flags.
            return self.connection.recv(1) == b""
        except (BlockingIOError, TimeoutError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
            return False
        except OSError:
            return True

    def proxy(self, body=None):
        target = urlsplit(self.server.config.tts_url)
        connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=120)
        try:
            headers = {"Content-Type": self.headers.get("Content-Type", "text/plain")}
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            chunked = response.getheader("Content-Length") is None
            for key, value in response.getheaders():
                if key.lower() in {"content-type", "content-length"} or key.lower().startswith("x-kokoro-"):
                    self.send_header(key, value)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while data := response.read1(65536):
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n" if chunked else data)
                self.wfile.flush()
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        finally:
            connection.close()

    def handle_request(self):
        path = urlsplit(self.path).path
        config = self.server.config
        if not self.path.startswith("/") or self.path.startswith("//"):
            raise RequestError(400, "Invalid request path.")
        if self.command == "GET" and path == "/":
            return self.reply(200, config.page.read_bytes(), "text/html; charset=utf-8")
        if self.command == "GET" and path in {"/chat", "/chat/", "/chat.js"}:
            asset = config.page.parent / ("chat.js" if path == "/chat.js" else "chat.html")
            return self.reply(200, asset.read_bytes(), "text/javascript; charset=utf-8" if path == "/chat.js" else "text/html; charset=utf-8")
        if self.command == "GET" and path == "/chat/health":
            registry = self.server.registry
            health = {"available": bool(getattr(config, "llm_url", None)),
                      "https_port": getattr(config, "https_port", None),
                      "tools": registry.names() if registry is not None else [],
                      "streaming": registry is not None}
            if registry is not None:
                status = registry.status()
                health["tool_sources"] = {row["name"]: row["source"] for row in status["tools"]}
                health["mcp"] = status["mcp"]
                health["retrieval"] = {key: status["retrieval"].get(key)
                                       for key in ("enabled", "ready", "stale", "reason", "chunks", "documents")
                                       if key in status["retrieval"]}
            return self.reply(200, health)
        if self.command == "GET" and path == "/tools":
            registry = self.server.registry
            if registry is None:
                return self.reply(200, {"tools": [], "mcp": [], "notes": [],
                                        "retrieval": {"enabled": False}})
            return self.reply(200, registry.status())
        if self.command == "GET" and path == "/stt/health":
            return self.reply(200, {"available": True, "busy": self.server.asr_lock.locked(),
                                    "https_port": getattr(config, "https_port", None),
                                    "backend": "qasr" if getattr(config, "asr_url", None) else "vvasr",
                                    "max_bytes": MAX_BYTES, "max_seconds": MAX_SECONDS})
        if self.command == "GET" and path == "/approvals":
            return self.approvals()
        if self.command == "GET" and path in {"/languages", "/voices", "/stats", "/health"}:
            return self.proxy()
        if self.command != "POST" or path not in {"/tts", "/stt", "/chat/completions", "/approvals"}:
            raise RequestError(404, "Not found.")
        if not self.same_origin():
            raise RequestError(403, "Use the speech UI on this server to submit audio or text.")
        if path == "/approvals":
            return self.approvals()
        if path == "/chat/completions":
            if not getattr(config, "llm_url", None):
                raise RequestError(503, "The conversation model is unavailable.")
            if not self.server.chat_lock.acquire(blocking=False):
                raise RequestError(503, "Another reply is in progress. Please try again shortly.")
            registry = self.server.registry
            wants_stream = ("application/x-ndjson" in (self.headers.get("Accept") or "")
                            or urlsplit(self.path).query in {"stream=1", "stream=true"}) and registry is not None
            progress = None
            try:
                body = self.body(voice_chat.MAX_BODY)
                self.connection.setblocking(False)
                try:
                    if registry is None:
                        result = voice_chat.complete(config.llm_url, body, self.disconnected)
                    else:
                        if wants_stream:
                            progress = Progress(self)

                        def report(event):
                            if event.get("type") == "answer":
                                return
                            progress.send(event)

                        result = voice_chat.turn(config.llm_url, body, self.disconnected, registry=registry,
                                                 limits=registry.config.limits,
                                                 on_event=report if wants_stream else None)
                except voice_chat.ChatError as error:
                    if progress is not None and progress.open:
                        progress.send({"type": "error", "status": error.status, "error": error.message})
                        progress.close()
                        self.connection.settimeout(30)
                        return
                    raise RequestError(error.status, error.message) from error
                self.connection.settimeout(30)
                if progress is not None:
                    progress.send({"type": "answer", "text": result["text"], "usage": result["usage"],
                                   "tools": result["tools"], "sources": result["sources"]})
                    progress.close()
                    return
                return self.reply(200, result)
            finally:
                self.server.chat_lock.release()
        if path == "/tts":
            return self.proxy(self.body(1024 * 1024))
        if not self.server.asr_lock.acquire(blocking=False):
            raise RequestError(503, "Transcription is busy. Please try again shortly.")
        try:
            audio = self.body(MAX_BYTES)
            # Disconnect polling must be nonblocking after the timed upload.
            self.connection.setblocking(False)
            with tempfile.TemporaryDirectory(prefix="speech-ui-") as directory:
                result = transcribe(config, audio, directory, self.disconnected,
                                    speech_ms(self.headers))
            self.connection.settimeout(30)
            self.reply(200, result)
        finally:
            self.server.asr_lock.release()

    def approvals(self):
        """The directory-grant queue.  GET reads it; POST is a human's click on it.

        This is the only code path in the deployment that can turn a request
        into a grant, so it is worth being explicit about why exposing it on the
        microphone's own port is not the hole it looks like:

        * It accepts no path.  The only decision it can express is
          approve/revoke against an id the assistant already filed, so a caller
          cannot conjure "let me read /home" -- they can only confirm a request
          a person can see on the page, with its file count and credential
          warnings attached.
        * It is same-origin, for the read as well as the write.  A random page
          the user visits can neither see which folders were asked about nor
          click on their behalf.
        * Revoking needs the path, not a secret.  That is deliberate: the point
          of the page is that access can be taken away immediately by whoever
          is looking at it.

        A stale id is a 400 rather than a 500 -- an approval card left open in
        two tabs is ordinary life, and the page just refetches the queue.
        """
        if not self.same_origin():
            raise RequestError(403, "Use the speech UI on this server to manage directory access.")
        store = approvals_store(self.server.registry)
        if store is None:
            return self.reply(200, {"enabled": False, "pending": [], "granted": []})
        if self.command == "GET":
            return self.reply(200, {"enabled": True, "pending": store.pending(),
                                    "granted": store.granted()})
        try:
            claim = json.loads(self.body(4096).decode("utf-8", "replace"))
        except ValueError as error:
            raise RequestError(400, "The approval request was not a valid JSON object.") from error
        if not isinstance(claim, dict):
            raise RequestError(400, "The approval request must be a JSON object.")
        decision = str(claim.get("decision") or "")
        try:
            if decision == "approve":
                outcome = store.approve(str(claim.get("id") or ""))
            elif decision == "decline":
                outcome = store.decline(str(claim.get("id") or ""))
            elif decision == "revoke":
                outcome = store.revoke(str(claim.get("path") or ""))
            else:
                raise RequestError(400, "decision must be 'approve', 'decline' or 'revoke'")
        except approvals.ApprovalError as error:
            raise RequestError(400, str(error)) from error
        return self.reply(200, {**outcome, "enabled": True, "pending": store.pending(),
                                "granted": store.granted()})

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            self.handle_request()
        except RequestError as error:
            if error.status != 499 and not self.response_started:
                try:
                    self.connection.settimeout(30)
                    # Busy/origin rejection happens before reading a valid
                    # upload. Drain its bounded body before closing TLS, or
                    # unread incoming records can reset the error response.
                    count = self.headers.get('Content-Length', '')
                    if not self.body_consumed and error.status in (403, 503) and count.isascii() and count.isdigit() and int(count) <= MAX_BYTES:
                        remaining = int(count)
                        while remaining:
                            data = self.rfile.read(min(remaining, 65536))
                            if not data:
                                break
                            remaining -= len(data)
                    self.reply(error.status, {"error": error.message})
                except OSError:
                    pass
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, http.client.HTTPException) as error:
            self.log_error("speech service error: %s", error)
            if not self.response_started:
                try:
                    self.reply(502, {"error": "Speech service unavailable. Check the server log."})
                except OSError:
                    pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--https-port", type=int)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--llm-url", help="local Qwen HTTP origin for voice conversations")
    parser.add_argument("--tts-url", default="http://127.0.0.1:8090")
    parser.add_argument("--page", type=Path, default=Path(__file__).resolve().parents[1] / "web/index.html")
    parser.add_argument("--asr-bin", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--tokenizer", type=Path)
    parser.add_argument("--asr-url",
                        help="resident qasr_serve origin, e.g. http://127.0.0.1:8095. "
                             "When set it replaces the per-request --asr-bin spawn; "
                             "leave it unset to keep native vvasr as the fallback.")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--tools-config", type=Path,
                        help="TOML describing tools, the RAG corpus and MCP servers. Without it the "
                             "assistant keeps its pre-tool behaviour exactly: no tools are offered.")
    config = parser.parse_args()
    native = (config.asr_bin, config.model, config.tokenizer)
    if config.asr_url and any(value is not None for value in native):
        parser.error("--asr-url replaces the native path; do not pass --asr-bin/--model/--tokenizer with it")
    if not config.asr_url and not all(value is not None for value in native):
        parser.error("ASR needs either --asr-url or the native --asr-bin/--model/--tokenizer trio")
    if config.asr_url:
        target = urlsplit(config.asr_url)
        if target.scheme != "http" or not target.hostname or target.path not in {"", "/"} \
                or target.query or target.fragment or target.username:
            parser.error("asr-url must be an HTTP origin")
    tls = None
    if any(value is not None for value in (config.https_port, config.tls_cert, config.tls_key)):
        if not all(value is not None for value in (config.https_port, config.tls_cert, config.tls_key)):
            parser.error("HTTPS requires --https-port, --tls-cert and --tls-key together")
        if not 1 <= config.https_port <= 65535 or config.https_port == config.port:
            parser.error("HTTPS requires a valid port distinct from the HTTP port")
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(config.tls_cert, config.tls_key)
    if config.llm_url:
        target = urlsplit(config.llm_url)
        if target.scheme != "http" or not target.hostname or target.path not in {"", "/"} or target.query or target.fragment or target.username:
            parser.error("llm-url must be an HTTP origin")
    # Native vvasr currently requires physical GPU 2. Do not silently remap it.
    # The claim belongs to the native path only: with --asr-url this process is
    # http.server plus ffmpeg and never initialises a device, so requiring the
    # variable there was a demand to *claim* a GPU rather than to use one -- and
    # it made the bridge impossible to start, or to test, on a machine with none.
    if not config.asr_url and os.environ.get("CUDA_VISIBLE_DEVICES") != "2":
        parser.error("native ASR requires authorized CUDA_VISIBLE_DEVICES=2; launch through the GPU guard")
    keys = ["page"] if config.asr_url else ["page", "asr_bin", "model", "tokenizer"]
    for key in keys:
        value = getattr(config, key).resolve()
        if not value.exists():
            parser.error(f"missing {key}: {value}")
        setattr(config, key, value)
    if not config.asr_url and not os.access(config.asr_bin, os.X_OK):
        parser.error("an executable native ASR binary and ffmpeg are required")
    if not shutil.which(config.ffmpeg):
        parser.error("ffmpeg is required: the bridge decodes uploads for either ASR backend")
    target = urlsplit(config.tts_url)
    if target.scheme != "http" or not target.hostname or target.path not in {"", "/"} or target.query:
        parser.error("tts-url must be an HTTP origin")
    registry = None
    if config.tools_config is not None:
        try:
            agent = agent_config.load(config.tools_config)
        except agent_config.ConfigError as error:
            parser.error(f"--tools-config: {error}")
        registry = agent_tools.Registry.build(agent, Path.cwd())
        for note in registry.notes:
            print(f"tools: {note}", flush=True)
        print(f"Tools offered: {', '.join(registry.names()) or 'none'}", flush=True)
    with SpeechServer((config.host, config.port), config, registry=registry) as server:
        secure_server = None
        if tls is not None:
            # The registry goes to both listeners.  The TLS one is the only
            # listener a microphone can use at all, so handing it the locks but
            # not the capabilities produced a page that looked deployed and
            # answered with nothing -- and the plain-HTTP health endpoint, which
            # is what a quick curl hits first, kept reporting every tool.
            secure_server = SpeechServer((config.host, config.https_port), config,
                                         tls=tls, asr_lock=server.asr_lock, chat_lock=server.chat_lock,
                                         registry=registry)
            threading.Thread(target=secure_server.serve_forever, daemon=True).start()
            print(f"Secure recording on https://{config.host}:{config.https_port}", flush=True)
        print(f"Speech UI listening on http://{config.host}:{config.port}", flush=True)
        # systemd and the supervisor both stop this process with SIGTERM.  Dying
        # by default would abandon every MCP child -- someone else's long-lived
        # process, still holding a stdio pipe to a dead parent -- so the signal
        # goes through the same shutdown path as a clean exit.
        stopping = threading.Event()

        def request_stop(signum, frame):
            stopping.set()

        installed = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, request_stop)
        watcher = threading.Thread(target=lambda: (stopping.wait(), server.shutdown()), daemon=True)
        watcher.start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, installed)
            if secure_server is not None:
                secure_server.shutdown()
                secure_server.server_close()
            if registry is not None:
                registry.close()


if __name__ == "__main__":
    main()
