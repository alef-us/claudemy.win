"""Render a two-host lesson script to one MP3.

Script format, one turn per line:
    A: So here's the thing about pricing...
    B: Right, and that's where most people...

Engine:
- ElevenLabs Text-to-Dialogue when ELEVENLABS_API_KEY is set. One call per
  batch, not isolated lines glued with silence (isolated lines sound robotic).
  Defaults: voices Liam + Jessica (energetic; calm voices read as flat),
  stability 0.3, model eleven_v3. Override with CLAUDEMY_VOICE_A,
  CLAUDEMY_VOICE_B, CLAUDEMY_STABILITY.
- Windows built-in voices otherwise (free, offline, plainer), through the
  System.Speech (SAPI 5) synthesizer in Windows PowerShell. Voices
  CLAUDEMY_WIN_VOICE_A / CLAUDEMY_WIN_VOICE_B, default "Microsoft Zira
  Desktop" and "Microsoft David Desktop"; a voice that isn't installed is
  replaced by an installed English voice of the same gender.
- macOS `say` when neither is available. Voices CLAUDEMY_SAY_A /
  CLAUDEMY_SAY_B, default Samantha and Daniel.
The free engines drop audio tags like [laughs], since they would read them aloud.
All paths finish with the same warm EQ and loudness pass in ffmpeg.

Usage: python render_audio.py <script.txt> <out.mp3>
"""
import base64, json, os, re, shutil, subprocess, sys, tempfile, urllib.error, urllib.request
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
INSTALL_HINT = "winget install Gyan.FFmpeg, then restart the terminal" if IS_WINDOWS else "brew install ffmpeg"

POLISH_AF = (
    "highpass=f=90,equalizer=f=200:t=q:w=1:g=-2,equalizer=f=3000:t=q:w=1:g=2.5,"
    "deesser,lowpass=f=13500,acompressor=ratio=2.5,aecho=0.8:0.5:40:0.08,loudnorm=I=-16"
)
BATCH_CHARS = 3000


def parse_turns(text):
    turns = []
    for line in text.splitlines():
        speaker, sep, said = line.partition(":")
        if sep and speaker.strip() in ("A", "B") and said.strip():
            turns.append((speaker.strip(), said.strip()))
    if not turns:
        raise ValueError("no 'A:' or 'B:' lines found in the script")
    return turns


def batches(turns):
    batch, size = [], 0
    for t in turns:
        if batch and size + len(t[1]) > BATCH_CHARS:
            yield batch
            batch, size = [], 0
        batch.append(t)
        size += len(t[1])
    yield batch


def elevenlabs_parts(turns, tmp, key):
    voices = {"A": os.environ.get("CLAUDEMY_VOICE_A", "TX3LPaxmHKxFdv7VOQHJ"),
              "B": os.environ.get("CLAUDEMY_VOICE_B", "cgSgspJ2msm6clMCkdW9")}
    stability = float(os.environ.get("CLAUDEMY_STABILITY", "0.3"))
    parts = []
    for i, batch in enumerate(batches(turns)):
        body = {"inputs": [{"text": said, "voice_id": voices[who]} for who, said in batch],
                "model_id": "eleven_v3", "settings": {"stability": stability}, "seed": 42}
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/text-to-dialogue", data=json.dumps(body).encode(),
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
        part = tmp / f"part{i}.mp3"
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                part.write_bytes(r.read())
        except urllib.error.HTTPError as e:
            # "HTTP Error 401" alone hides the cause (bad key, quota, unknown voice id);
            # the API says which in the response body. The key is never printed.
            detail = e.read().decode("utf-8", "replace").strip()[:300]
            sys.exit(f"ElevenLabs refused batch {i + 1}: HTTP {e.code} {e.reason}. {detail}")
        parts.append(part)
    return parts


def drop_tags(said):
    return re.sub(r"\[[^\]]+\]", "", said)


def say_parts(turns, tmp):
    voices = {"A": os.environ.get("CLAUDEMY_SAY_A", "Samantha"),
              "B": os.environ.get("CLAUDEMY_SAY_B", "Daniel")}
    parts = []
    for i, (who, said) in enumerate(turns):
        part = tmp / f"turn{i:03d}.aiff"
        subprocess.run(["say", "-v", voices[who], "-o", str(part), drop_tags(said)], check=True)
        parts.append(part)
    return parts


# One PowerShell process renders every turn: starting one per turn costs about a
# second each. The text travels in a UTF-8 JSON file, never on the command line,
# so quotes, dollar signs and non-Latin letters reach the synthesizer as written.
SAPI_PS1 = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$job = [IO.File]::ReadAllText($JobFile, [Text.Encoding]::UTF8) | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$installed = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo })
if ($installed.Count -eq 0) { [Console]::Error.WriteLine('no Windows voices are installed'); exit 3 }
function Pick($want, $gender) {
    $v = $installed | Where-Object { $_.Name -eq $want } | Select-Object -First 1
    if (-not $v) { $v = $installed | Where-Object { $_.Name -like "*$want*" } | Select-Object -First 1 }
    if (-not $v) { $v = $installed | Where-Object { "$($_.Gender)" -eq $gender -and $_.Culture.Name -like 'en*' } | Select-Object -First 1 }
    if (-not $v) { $v = $installed | Where-Object { $_.Culture.Name -like 'en*' } | Select-Object -First 1 }
    if (-not $v) { $v = $installed[0] }
    $v.Name
}
$names = @{ A = (Pick $job.voices.A.want $job.voices.A.gender); B = (Pick $job.voices.B.want $job.voices.B.gender) }
foreach ($t in $job.turns) {
    $synth.SelectVoice($names[$t.who])
    $synth.SetOutputToWaveFile($t.out)
    $synth.Speak($t.text)
}
$synth.SetOutputToNull()
$synth.Dispose()
Write-Output "A=$($names.A)"
Write-Output "B=$($names.B)"
"""


def sapi_parts(turns, tmp):
    voices = {"A": {"want": os.environ.get("CLAUDEMY_WIN_VOICE_A", "Microsoft Zira Desktop"), "gender": "Female"},
              "B": {"want": os.environ.get("CLAUDEMY_WIN_VOICE_B", "Microsoft David Desktop"), "gender": "Male"}}
    parts = [tmp / f"turn{i:03d}.wav" for i in range(len(turns))]
    job = {"voices": voices,
           "turns": [{"who": who, "text": drop_tags(said), "out": str(p)} for (who, said), p in zip(turns, parts)]}
    job_file = tmp / "sapi.json"
    job_file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    # -EncodedCommand, not a .ps1 file: a Group Policy execution policy can block
    # script files even with -ExecutionPolicy Bypass, but never an encoded command.
    script = "$JobFile = '" + str(job_file).replace("'", "''") + "'\n" + SAPI_PS1
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                        base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not all(p.exists() for p in parts):
        sys.exit(f"Windows voices failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:500]}")
    picked = dict(l.split("=", 1) for l in r.stdout.splitlines() if l[:2] in ("A=", "B="))
    if picked.get("A") and picked.get("A") == picked.get("B"):
        print(f"note: both hosts got the same Windows voice ({picked['A']}); set CLAUDEMY_WIN_VOICE_A and "
              "CLAUDEMY_WIN_VOICE_B to two different installed voices, or add an English voice in "
              "Settings > Time & language > Speech", file=sys.stderr)
    return parts


def pick_engine(key):
    if key:
        return "ElevenLabs"
    if IS_WINDOWS and shutil.which("powershell.exe"):
        return "Windows voices"
    if shutil.which("say"):
        return "macOS say"
    return None


def stitch(parts, tmp, out_path):
    # Bare file names, resolved next to list.txt: an absolute path in the list breaks
    # on an apostrophe, and on non-Latin letters once the list leaves UTF-8.
    listing = tmp / "list.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-af", POLISH_AF, "-ar", "44100", "-b:a", "128k", str(out_path)], check=True)


def main(script_path, out_path):
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"missing dependency: {tool} ({INSTALL_HINT})")
    # utf-8-sig: Notepad and Windows PowerShell 5.1 start UTF-8 files with a BOM,
    # which would otherwise hide the first "A:" and drop that turn.
    turns = parse_turns(Path(script_path).read_text(encoding="utf-8-sig"))
    key = os.environ.get("ELEVENLABS_API_KEY")
    engine = pick_engine(key)
    if engine is None:
        sys.exit("no audio engine: set ELEVENLABS_API_KEY (or the plugin's ElevenLabs key option), "
                 "or run on Windows or macOS for the built-in voices")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        if engine == "ElevenLabs":
            parts = elevenlabs_parts(turns, tmp, key)
        elif engine == "Windows voices":
            parts = sapi_parts(turns, tmp)
        else:
            parts = say_parts(turns, tmp)
        stitch(parts, tmp, out_path)
    length = stamp_card(Path(script_path).with_name("card.md"), out_path)
    print(f"wrote {out_path} ({length}) from {len(turns)} turns via {engine}")


def stamp_card(card, mp3):
    """Write the real running time onto the card's Listen line. A hand-typed
    "(2 min)" sat on a 2:32 lesson in the first full test run."""
    secs = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 str(mp3)], capture_output=True, text=True, check=True).stdout.strip())
    length = f"{int(secs // 60)}:{int(secs % 60):02d}"
    if card.exists():
        # Bytes in, bytes out: the card keeps its BOM and line endings (CRLF on Windows).
        raw = card.read_bytes()
        bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        text = raw[len(bom):].decode("utf-8")
        text = re.sub(r"^(\*\*Listen:\*\*.*?)[ \t]*\([^)\r\n]*\)[ \t]*(\r?)$", rf"\1 ({length})\2", text,
                      count=1, flags=re.M)
        card.write_bytes(bom + text.encode("utf-8"))
    return length


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    main(sys.argv[1], sys.argv[2])
