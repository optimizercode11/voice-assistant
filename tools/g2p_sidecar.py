#!/usr/bin/env python3
"""misaki/KPipeline G2P as its own process (design decision D4).

kserver's GPU lane must never wait on phonemization, so text->IPA runs here, on
a Unix socket, in the CPU-only oracle venv.  One connection per request:

    request : "<len>\\n" + text
    response: "<len>\\n" + <segment>("\\n"<segment>)*

The response is newline-separated because KPipeline segments a document itself
-- the canonical long paragraph comes back as 415 + 379 phonemes -- and that
segmentation is part of what the oracle measured.  kserver must not re-cut it.

This uses KPipeline(lang_code=..., model=False), the same call path
tools/g2p_dump.py used to build corpus.tsv, so the sidecar and the corpus agree
by construction rather than by luck.  (An earlier revision called
misaki.en.MisakiG2P directly; that class does not exist in misaki 0.9.4 and the
sidecar died on import, which is why /tts had never actually been exercised.)

    CUDA_VISIBLE_DEVICES="" /mnt/kokoro/oracle-env/bin/python \\
        tools/g2p_sidecar.py --socket /tmp/kokoro-g2p.sock
"""
import argparse
import os
import socket
import sys
import threading

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # this lane is CPU-only by fiat


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="/tmp/kokoro-g2p.sock")
    ap.add_argument("--lang", default="a")  # KPipeline 'a' == American English
    ap.add_argument("--model-dir", default="/mnt/kokoro")
    ap.add_argument("--languages", default="", help="Preload Kokoro language codes, e.g. abefhipjz")
    ap.add_argument("--cpu", action="store_true", help="Explicit CPU-only execution")
    args = ap.parse_args()

    if os.environ.get("CUDA_VISIBLE_DEVICES") != "":
        raise RuntimeError("G2P requires CUDA_VISIBLE_DEVICES empty")
    import torch  # noqa: E402  (CPU-only oracle venv)
    torch.set_grad_enabled(False)
    torch.set_num_threads(2)
    from kokoro import KPipeline  # noqa: E402

    codes = args.languages or args.lang
    if any(c not in "abefhipjz" for c in codes) or args.lang not in codes:
        raise ValueError("Unsupported language list or missing default language")
    pipes = {code: KPipeline(lang_code=code, repo_id="hexgrad/Kokoro-82M", model=False, device="cpu")
             for code in dict.fromkeys(codes)}

    def phonemize(text: str, language: str) -> list[str]:
        pipe = pipes[language]
        out = []
        for line in text.splitlines():
            if not line.strip():
                continue
            if language not in "ab":
                # KPipeline caps non-English phoneme strings at 510. Preserve
                # the complete pronunciation; kserver segments it by UTF-8.
                ps, _ = pipe.g2p(line)
                if ps: out.append(ps.replace("\n", " ").strip())
                continue
            for r in pipe(line):
                ps = getattr(r, "phonemes", None)
                if ps is None:      # kokoro 0.9.x yields (graphemes, phonemes, tokens)
                    ps = r[1]
                if ps:
                    out.append(ps.replace("\n", " ").strip())
        return out

    lock = threading.Lock()  # the spaCy pipeline is not reentrant

    samples = dict(a="Hello world.", b="Hello world.", e="Hola mundo.", f="Bonjour le monde.",
                   h="नमस्ते दुनिया।", i="Ciao mondo.", p="Olá mundo.", j="こんにちは世界。", z="你好世界。")
    for code in pipes:
        warm = phonemize(samples[code], code)
        if not warm: raise RuntimeError(f"No phonemes for language {code}")
        print(f"g2p-sidecar: warm {code}, {warm[0][:40]!r}", file=sys.stderr, flush=True)

    if os.path.exists(args.socket):
        raise RuntimeError("Refusing to replace an existing G2P socket")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(args.socket)
    os.chmod(args.socket, 0o660)
    srv.listen(64)
    print(f"g2p-sidecar: listening on {args.socket}", file=sys.stderr, flush=True)

    def read_msg(c: socket.socket):
        head = b""
        while not head.endswith(b"\n"):
            b = c.recv(1)
            if not b:
                return None
            head += b
            if len(head) > 64: raise ValueError("Frame header too long")
        fields = head.decode("ascii").split()
        if len(fields) not in (1, 2): raise ValueError("Invalid frame header")
        n = int(fields[0])
        if not 0 <= n <= 1048576: raise ValueError("Invalid frame length")
        language = fields[1] if len(fields) == 2 else args.lang
        buf = b""
        while len(buf) < n:
            b = c.recv(n - len(buf))
            if not b:
                return None
            buf += b
        return language, buf.decode("utf-8")

    def serve(c: socket.socket):
        try:
            c.settimeout(30)
            msg = read_msg(c)
            if msg is None: return
            language, text = msg
            if language == "?":
                out = "".join(pipes).encode("ascii")
            else:
                if language not in pipes: raise ValueError("Unsupported language")
                with lock:
                    segs = phonemize(text, language)
                out = "\n".join(segs).encode("utf-8")
            c.sendall(b"%d\n" % len(out) + out)
        except Exception as e:  # never take the socket down for one bad line
            print(f"g2p-sidecar: {e!r}", file=sys.stderr, flush=True)
            try: c.sendall(b"-1\n")
            except OSError: pass
        finally:
            c.close()

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=serve, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
